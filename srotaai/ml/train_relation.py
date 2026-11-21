"""Train a drug–event assertion classifier from annotated mention pairs.

JSONL: {"text":"...", "group":"document-id", "drug":[start,end],
"event":[start,end], "label":"adverse|negated|speculative|co_mention"}
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
from sklearn.feature_extraction.text import FeatureUnion, TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import classification_report
from sklearn.model_selection import GroupShuffleSplit
from sklearn.pipeline import Pipeline

from .relation import pair_text

LABELS = {"adverse", "negated", "speculative", "co_mention"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("data", type=Path)
    ap.add_argument("--output", type=Path, default=Path("models/ade_relation.joblib"))
    args = ap.parse_args()
    rows = [json.loads(x) for x in args.data.read_text(encoding="utf-8").splitlines() if x.strip()]
    for i, row in enumerate(rows, 1):
        text = row.get("text", "")
        if not text or not row.get("group") or row.get("label") not in LABELS:
            raise ValueError(f"row {i}: requires text, group, and a supported relation label")
        for key in ("drug", "event"):
            span = row.get(key, [])
            if len(span) != 2 or not (0 <= span[0] < span[1] <= len(text)):
                raise ValueError(f"row {i}: invalid {key} offsets")
    if {r["label"] for r in rows} != LABELS:
        raise ValueError(f"Data must contain all four classes: {sorted(LABELS)}")

    x = np.asarray([pair_text(r["text"], tuple(r["drug"]), tuple(r["event"])) for r in rows], dtype=object)
    y = np.asarray([r["label"] for r in rows], dtype=object)
    groups = np.asarray([r["group"] for r in rows], dtype=object)
    indices = np.arange(len(rows))
    for seed in range(100):
        a, test = next(GroupShuffleSplit(n_splits=1, test_size=.2, random_state=42+seed).split(indices, y, groups))
        train_rel, valid_rel = next(GroupShuffleSplit(n_splits=1, test_size=.1875, random_state=1042+seed).split(a, y[a], groups[a]))
        train, valid = a[train_rel], a[valid_rel]
        if all(set(y[idx]) == set(y) for idx in (train, valid, test)):
            assert not (set(groups[train]) & set(groups[valid]) or set(groups[train]) & set(groups[test]) or set(groups[valid]) & set(groups[test]))
            break
    else:
        raise ValueError("Could not create group-disjoint train/validation/test splits covering all labels")

    pipeline = Pipeline([
        ("features", FeatureUnion([
            ("word", TfidfVectorizer(ngram_range=(1,2), min_df=2, max_features=100_000)),
            ("char", TfidfVectorizer(analyzer="char_wb", ngram_range=(3,5), min_df=2, max_features=150_000)),
        ])),
        ("classifier", LogisticRegression(class_weight="balanced", max_iter=2000)),
    ])
    pipeline.fit(x[train], y[train])
    report = {
        "validation": classification_report(y[valid], pipeline.predict(x[valid]),
                                            labels=sorted(LABELS), output_dict=True,
                                            zero_division=0),
        "test": classification_report(y[test], pipeline.predict(x[test]),
                                       labels=sorted(LABELS), output_dict=True,
                                       zero_division=0),
        "partition_sizes": {"train": len(train), "validation": len(valid), "test": len(test)},
        "group_counts": {"train": len(set(groups[train])),
                         "validation": len(set(groups[valid])),
                         "test": len(set(groups[test]))},
        "split_seed": 42 + seed,
        "labels": sorted(LABELS),
    }
    print(json.dumps(report, indent=2))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({"pipeline": pipeline, "model_version": datetime.now(timezone.utc).strftime("relation-tfidf-%Y%m%dT%H%M%SZ"), "metrics": report}, args.output)


if __name__ == "__main__":
    main()
