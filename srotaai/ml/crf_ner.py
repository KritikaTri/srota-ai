"""Inference for a trained, character-aware CRF drug/event span tagger."""
from __future__ import annotations

import os
import re
from pathlib import Path

import joblib

DEFAULT_MODEL = Path(__file__).resolve().parents[2] / "models" / "ade_ner_crf.joblib"
TOKEN_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)
_MODEL = None
_TRIED = False


def tokenize(text: str):
    return [(m.group(), m.start(), m.end()) for m in TOKEN_RE.finditer(text)]


def token_features(tokens, i):
    word = tokens[i][0]
    lower = word.lower()
    features = {
        "bias": 1.0, "word.lower": lower,
        "word[-3:]": lower[-3:], "word[-2:]": lower[-2:],
        "word[:2]": lower[:2], "word[:3]": lower[:3],
        "word.isupper": word.isupper(), "word.istitle": word.istitle(),
        "word.isdigit": word.isdigit(), "word.hasdigit": any(c.isdigit() for c in word),
        "word.shape": "".join("X" if c.isupper() else "x" if c.islower() else "d" if c.isdigit() else c for c in word),
    }
    for offset in (-2, -1, 1, 2):
        j = i + offset
        if 0 <= j < len(tokens):
            neighbor = tokens[j][0].lower()
            features[f"{offset}:word.lower"] = neighbor
            features[f"{offset}:word.istitle"] = tokens[j][0].istitle()
    if i == 0:
        features["BOS"] = True
    if i == len(tokens) - 1:
        features["EOS"] = True
    return features


def _load():
    global _MODEL, _TRIED
    if _TRIED:
        return _MODEL
    _TRIED = True
    path = Path(os.getenv("SROTAAI_NER_MODEL", str(DEFAULT_MODEL)))
    if path.is_file():
        loaded = joblib.load(path)
        _MODEL = loaded.get("model") if isinstance(loaded, dict) else loaded
    return _MODEL


def predict(text: str):
    model = _load()
    if model is None or not text.strip():
        return None
    tokens = tokenize(text)
    if not tokens:
        return []
    features = [token_features(tokens, i) for i in range(len(tokens))]
    tags = model.predict_single(features)
    marginals = model.predict_marginals_single(features)
    found = []
    active = None
    for (word, start, end), tag, distribution in zip(tokens, tags, marginals):
        if tag == "O":
            if active:
                found.append(active)
                active = None
            continue
        prefix, kind = tag.split("-", 1)
        confidence = float(distribution.get(tag, 0.0))
        if prefix == "B" or active is None or active["label"] != kind:
            if active:
                found.append(active)
            active = {"label": kind, "start": start, "end": end,
                      "text": text[start:end], "_conf": [confidence]}
        else:
            active["end"] = end
            active["text"] = text[active["start"]:end]
            active["_conf"].append(confidence)
    if active:
        found.append(active)
    for item in found:
        scores = item.pop("_conf")
        item["confidence"] = sum(scores) / len(scores)
    return found
