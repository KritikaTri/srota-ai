"""Evaluate trained components against a manually annotated, untouched JSONL.

Each row: group, split, text, ade_label, entities[{start,end,label}],
relations[{drug:[start,end],event:[start,end],label}]. Run only on split=test.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from sklearn.metrics import classification_report

from .ade_classifier import load_bundle as load_ade, predict_text
from .crf_ner import predict as predict_entities
from .relation import predict as predict_relation


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("gold", type=Path)
    parser.add_argument("--split", default="test")
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.gold.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    rows = [r for r in rows if r.get("split") == args.split]
    if not rows:
        raise SystemExit(f"No rows for split={args.split!r}")
    if any(not r.get("group") or not r.get("text") or r.get("ade_label") not in {"ade", "not_ade"}
           for r in rows):
        raise ValueError("Each row needs group, text, and ade_label=ade|not_ade")

    ade_bundle = load_ade()
    if ade_bundle is None:
        raise SystemExit("No ADE model loaded; train one before evaluating")
    ade_true, ade_pred = [], []
    entity_true, entity_pred = set(), set()
    relation_true, relation_pred = [], []
    has_relation_model = False
    for doc_index, row in enumerate(rows):
        doc_key = (row["group"], doc_index)
        ade_true.append(row["ade_label"])
        ade_pred.append(predict_text(row["text"], ade_bundle)["label"])

        gold_spans = {(doc_key, e["start"], e["end"], e["label"])
                      for e in row.get("entities", [])}
        predicted_spans = {(doc_key, e["start"], e["end"], e["label"])
                           for e in (predict_entities(row["text"]) or [])}
        entity_true |= gold_spans
        entity_pred |= predicted_spans

        for relation in row.get("relations", []):
            result = predict_relation(row["text"], tuple(relation["drug"]), tuple(relation["event"]))
            if result is not None:
                has_relation_model = True
                relation_true.append(relation["label"])
                relation_pred.append(result["label"])

    exact = len(entity_true & entity_pred)
    entity_report = {
        "exact_span_precision": exact / len(entity_pred) if entity_pred else 0.0,
        "exact_span_recall": exact / len(entity_true) if entity_true else 0.0,
        "exact_span_f1": 2 * exact / (len(entity_true) + len(entity_pred))
        if entity_true or entity_pred else 0.0,
        "gold_spans": len(entity_true), "predicted_spans": len(entity_pred),
    }
    output = {
        "split": args.split, "documents": len(rows),
        "ade": classification_report(ade_true, ade_pred,
                                     labels=["ade", "not_ade"], output_dict=True,
                                     zero_division=0),
        "entities": entity_report,
        "relation": classification_report(
            relation_true, relation_pred,
            labels=["adverse", "negated", "speculative", "co_mention"],
            output_dict=True, zero_division=0) if has_relation_model else None,
        "relation_note": "Conditional on gold entity spans; relation results are omitted until a model is trained."
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
