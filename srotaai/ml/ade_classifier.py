"""ADE report classifier using a supervised TF-IDF + logistic regression model.

The classifier is deliberately separate from lexicon sentiment and statistical
signal detection. It estimates whether a post reports an adverse drug event;
it does not establish causality or make clinical decisions.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

DEFAULT_MODEL_PATH = Path(__file__).resolve().parents[2] / "models" / "ade_classifier.joblib"


def load_bundle(path: str | Path | None = None) -> dict[str, Any] | None:
    model_path = Path(path or os.getenv("SROTAAI_ADE_MODEL", DEFAULT_MODEL_PATH))
    if not model_path.is_file():
        return None
    try:
        import joblib
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("Install the 'ml' requirements to load the ADE model") from exc
    bundle = joblib.load(model_path)
    if not isinstance(bundle, dict) or "pipeline" not in bundle:
        raise ValueError(f"Invalid ADE model bundle: {model_path}")
    return bundle


def predict_text(text: str, bundle: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Return a positive-class probability, or None until a trained model exists."""
    bundle = bundle if bundle is not None else load_bundle()
    if bundle is None or not text.strip():
        return None
    pipeline = bundle["pipeline"]
    classes = list(pipeline.classes_)
    positive_label = str(bundle.get("positive_label", "ade"))
    if positive_label not in classes:
        raise ValueError(f"Model is missing positive label {positive_label!r}")
    probs = pipeline.predict_proba([text])[0]
    positive_probability = float(probs[classes.index(positive_label)])
    threshold = float(bundle.get("threshold", 0.5))
    return {
        "probability": positive_probability,
        "label": positive_label if positive_probability >= threshold else "not_ade",
        "threshold": threshold,
        "model_version": str(bundle.get("model_version", "unknown")),
    }
