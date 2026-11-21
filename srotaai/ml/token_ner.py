"""Optional Transformer token-classification inference for DRUG / EVENT spans."""
from __future__ import annotations

import os
from pathlib import Path

MODEL_PATH = Path(__file__).resolve().parents[2] / "models" / "ade_ner"
_PIPE = None
_TRIED = False


def _load():
    global _PIPE, _TRIED
    if _TRIED:
        return _PIPE
    _TRIED = True
    path = Path(os.getenv("SROTAAI_NER_MODEL", str(MODEL_PATH)))
    if not path.exists():
        return None
    try:
        from transformers import pipeline
        _PIPE = pipeline("token-classification", model=str(path), tokenizer=str(path),
                         aggregation_strategy="simple")
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("Install requirements-ml.txt to load the Transformer NER model") from exc
    return _PIPE


def predict(text: str) -> list[dict] | None:
    pipe = _load()
    if pipe is None or not text.strip():
        return None
    found = []
    for item in pipe(text):
        label = str(item.get("entity_group", item.get("entity", ""))).upper()
        if label in {"DRUG", "EVENT"}:
            found.append({"label": label, "text": item["word"],
                          "start": int(item["start"]), "end": int(item["end"]),
                          "confidence": float(item["score"])})
    return found
