"""Optional supervised drug–event assertion classifier."""
from __future__ import annotations

import os
from pathlib import Path

MODEL_PATH = Path(__file__).resolve().parents[2] / "models" / "ade_relation.joblib"
_BUNDLE = None
_TRIED = False


def pair_text(text: str, drug_span: tuple[int, int], event_span: tuple[int, int]) -> str:
    spans = [(drug_span[0], drug_span[1], "DRUG"),
             (event_span[0], event_span[1], "EVENT")]
    for start, end, label in sorted(spans, reverse=True):
        text = text[:end] + f"</{label}>" + text[end:]
        text = text[:start] + f"<{label}>" + text[start:]
    return text


def predict(text: str, drug_span: tuple[int, int], event_span: tuple[int, int]):
    global _BUNDLE, _TRIED
    if not _TRIED:
        _TRIED = True
        path = Path(os.getenv("SROTAAI_RELATION_MODEL", str(MODEL_PATH)))
        if path.is_file():
            try:
                import joblib
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError("Install scikit-learn/joblib to load relation model") from exc
            _BUNDLE = joblib.load(path)
    if _BUNDLE is None:
        return None
    sample = pair_text(text, drug_span, event_span)
    model = _BUNDLE["pipeline"]
    probs = model.predict_proba([sample])[0]
    index = int(probs.argmax())
    label = str(model.classes_[index])
    return {"label": label, "probability": float(probs[index]),
            "model_version": str(_BUNDLE.get("model_version", "unknown"))}
