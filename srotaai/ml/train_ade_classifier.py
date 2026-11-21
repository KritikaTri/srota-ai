"""Train and evaluate the ADE classifier from licensed, human-labelled JSONL.

Input rows: {"text": "...", "label": "ade|not_ade", "group": "source/document id"}
The group is required so near-duplicate posts from one source/document cannot
leak across train/validation/test partitions.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics import (average_precision_score, classification_report,
                             precision_recall_curve)
from sklearn.model_selection import GroupShuffleSplit
from sklearn.pipeline import FeatureUnion, Pipeline

from srotaai import ner, sentiment
from sklearn.linear_model import LogisticRegression


def read_jsonl(path: Path):
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for i, row in enumerate(rows, 1):
        if not isinstance(row.get("text"), str) or not row["text"].strip():
            raise ValueError(f"row {i}: text is required")
        if row.get("label") not in {"ade", "not_ade"}:
            raise ValueError(f"row {i}: label must be 'ade' or 'not_ade'")
        if not row.get("group"):
            raise ValueError(f"row {i}: group is required for leakage-safe splitting")
    if len({r["label"] for r in rows}) != 2:
        raise ValueError("Training data must contain both labels")
    return rows


def group_split(rows, test_size=0.2, valid_size=0.15, seed=42):
    texts = np.asarray([r["text"] for r in rows], dtype=object)
    labels = np.asarray([r["label"] for r in rows], dtype=object)
    groups = np.asarray([r["group"] for r in rows], dtype=object)
    for attempt in range(100):
        rs = seed + attempt
        first = GroupShuffleSplit(n_splits=1, test_size=test_size, random_state=rs)
        train_valid, test = next(first.split(texts, labels, groups))
        second = GroupShuffleSplit(n_splits=1, test_size=valid_size / (1 - test_size), random_state=rs + 1000)
        train_rel, valid_rel = next(second.split(texts[train_valid], labels[train_valid], groups[train_valid]))
        train = train_valid[train_rel]
        valid = train_valid[valid_rel]
        if all(set(labels[ix]) == {"ade", "not_ade"} for ix in (train, valid, test)):
            if (set(groups[train]) & set(groups[valid]) or set(groups[train]) & set(groups[test])
                    or set(groups[valid]) & set(groups[test])):
                raise AssertionError("group leakage in split")
            return texts, labels, train, valid, test
    raise ValueError("Could not make group-disjoint splits containing both labels; add more independent groups")


def make_pipeline():
    features = FeatureUnion([
        ("word", TfidfVectorizer(ngram_range=(1, 2), min_df=2, sublinear_tf=True,
                                 strip_accents="unicode", max_features=150_000)),
        ("char", TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), min_df=2,
                                 sublinear_tf=True, max_features=200_000)),
    ])
    classifier = LogisticRegression(class_weight="balanced", max_iter=2000,
                                    solver="liblinear", random_state=42)
    return Pipeline([("features", features), ("classifier", classifier)])


def best_f1_threshold(y_true, probabilities):
    precision, recall, thresholds = precision_recall_curve(y_true, probabilities, pos_label="ade")
    if not len(thresholds):
        return 0.5
    f1 = 2 * precision[:-1] * recall[:-1] / np.maximum(precision[:-1] + recall[:-1], 1e-12)
    return float(thresholds[int(np.argmax(f1))])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("data", type=Path, help="JSONL with text,label,group")
    parser.add_argument("--output", type=Path, default=Path("models/ade_classifier.joblib"))
    args = parser.parse_args()

    rows = read_jsonl(args.data)
    texts, labels, train, valid, test = group_split(rows)
    model = make_pipeline()
    model.fit(texts[train], labels[train])

    positive_idx = list(model.classes_).index("ade")
    valid_prob = model.predict_proba(texts[valid])[:, positive_idx]
    threshold = best_f1_threshold(labels[valid], valid_prob)
    test_prob = model.predict_proba(texts[test])[:, positive_idx]
    test_pred = np.where(test_prob >= threshold, "ade", "not_ade")
    report = classification_report(labels[test], test_pred, labels=["ade", "not_ade"],
                                   output_dict=True, zero_division=0)
    rule_predictions = []
    for index in test:
        text = texts[index]
        entities = ner.extract(text)
        is_ade = bool(entities.drug_event_pairs) and sentiment.analyze(text).label in ("adverse", "negative")
        rule_predictions.append("ade" if is_ade else "not_ade")
    report["rule_baseline"] = classification_report(
        labels[test], rule_predictions, labels=["ade", "not_ade"],
        output_dict=True, zero_division=0,
    )
    report["ade_average_precision"] = float(average_precision_score(labels[test] == "ade", test_prob))
    report["threshold_selected_on_validation"] = threshold
    report["counts"] = {"all": len(rows), "train": len(train), "validation": len(valid), "test": len(test)}
    report["group_counts"] = {"train": len(set(r["group"] for r in np.asarray(rows, dtype=object)[train])),
                              "validation": len(set(r["group"] for r in np.asarray(rows, dtype=object)[valid])),
                              "test": len(set(r["group"] for r in np.asarray(rows, dtype=object)[test]))}
    print(json.dumps(report, indent=2))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({"pipeline": model, "positive_label": "ade", "threshold": threshold,
                 "model_version": datetime.now(timezone.utc).strftime("ade-tfidf-%Y%m%dT%H%M%SZ"),
                 "metrics": report}, args.output)
    print(f"Saved model bundle: {args.output}")


if __name__ == "__main__":
    main()
