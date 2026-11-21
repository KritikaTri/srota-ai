"""Train/evaluate a group-split CRF drug/event extractor from BRAT JSONL."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import joblib
import numpy as np
from sklearn.model_selection import GroupShuffleSplit

from .crf_ner import token_features, tokenize
from .train_token_ner import read_rows


def labels_for(tokens, entities):
    tags = ["O"] * len(tokens)
    excluded = 0
    # A flat BIO model cannot represent nested/overlapping mentions. Prefer
    # longer spans, then mark any conflicting shorter annotations excluded.
    occupied = set()
    for entity in sorted(entities, key=lambda e: (-(e["end"] - e["start"]), e["start"])):
        covered = [i for i, (_, start, end) in enumerate(tokens)
                   if end > entity["start"] and start < entity["end"]]
        if not covered:
            excluded += 1
            continue
        if any(j in occupied for j in covered):
            excluded += 1
            continue
        occupied.update(covered)
        kind = entity["label"]
        tags[covered[0]] = "B-" + kind
        for j in covered[1:]:
            tags[j] = "I-" + kind
    return tags, excluded


def as_spans(tags, tokens):
    spans = set()
    active = None
    for tag, (_, start, end) in zip(tags, tokens):
        if tag == "O":
            if active:
                spans.add(tuple(active))
                active = None
            continue
        prefix, kind = tag.split("-", 1)
        if prefix == "B" or active is None or active[2] != kind:
            if active:
                spans.add(tuple(active))
            active = [start, end, kind]
        else:
            active[1] = end
    if active:
        spans.add(tuple(active))
    return spans


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("data", type=Path)
    parser.add_argument("--output", type=Path, default=Path("models/ade_ner_crf.joblib"))
    args = parser.parse_args()
    rows = read_rows(args.data)
    groups = np.asarray([r["group"] for r in rows], dtype=object)
    indices = np.arange(len(rows))
    for seed in range(42, 142):
        train_valid, test = next(GroupShuffleSplit(n_splits=1, test_size=.2,
                                                   random_state=seed).split(indices, groups=groups))
        train_rel, valid_rel = next(GroupShuffleSplit(n_splits=1, test_size=.1875,
                                                       random_state=seed + 1000).split(
                                                           train_valid, groups=groups[train_valid]))
        train, valid = train_valid[train_rel], train_valid[valid_rel]
        if all(any(rows[i].get("entities") for i in part) for part in (train, valid, test)):
            break
    else:
        raise ValueError("Could not create group-disjoint splits containing spans")

    encoded = []
    excluded_annotations = 0
    for row in rows:
        tokens = tokenize(row["text"])
        tags, excluded = labels_for(tokens, row.get("entities", []))
        excluded_annotations += excluded
        encoded.append((tokens, [token_features(tokens, i) for i in range(len(tokens))], tags))

    from sklearn_crfsuite import CRF
    model = CRF(algorithm="lbfgs", c1=.1, c2=.1, max_iterations=150,
                all_possible_transitions=True)
    model.fit([encoded[i][1] for i in train], [encoded[i][2] for i in train])
    # Validation documents remain untouched by fitting; report threshold-free
    # exact span metrics for validation and held-out test.
    def evaluate(part):
        true, predicted = set(), set()
        for i in part:
            row = rows[i]
            tokens, features, gold_tags = encoded[i]
            doc = str(i)
            gold = {(doc, *span) for span in as_spans(gold_tags, tokens)}
            pred = {(doc, *span) for span in as_spans(model.predict_single(features), tokens)}
            true |= gold
            predicted |= pred
        # Exact micro counts are easier to interpret and support both entity types.
        correct = len(true & predicted)
        return {"exact_span_precision": correct / len(predicted) if predicted else 0.0,
                "exact_span_recall": correct / len(true) if true else 0.0,
                "exact_span_f1": 2 * correct / (len(true) + len(predicted)) if true or predicted else 0.0,
                "gold_spans": len(true), "predicted_spans": len(predicted)}

    metrics = {"validation": evaluate(valid), "test": evaluate(test),
               "documents": {"all": len(rows), "train": len(train),
                             "validation": len(valid), "test": len(test)},
               "split_seed": seed, "labels": ["DRUG", "EVENT"],
               "overlapping_or_unaligned_annotations_excluded": excluded_annotations}
    # keep `metrics` in artifact for reproducible reporting
    args.output.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": model, "metrics": metrics, "labels": ["DRUG", "EVENT"]}, args.output)
    print(json.dumps(metrics, indent=2))
    print(f"Saved model: {args.output}")


if __name__ == "__main__":
    main()
