"""Validate human relation labels and export the training JSONL format."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

LABELS = {"adverse", "negated", "speculative", "co_mention"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("queue", type=Path)
    parser.add_argument("--output", type=Path, default=Path("data/ml/ade_relations.jsonl"))
    parser.add_argument("--allow-incomplete", action="store_true",
                        help="Export reviewed rows even if some remain unreviewed or a class is missing")
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.queue.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    seen = set()
    exported = []
    unreviewed = 0
    counts = Counter()
    for i, row in enumerate(rows, 1):
        row_id = row.get("row_id")
        if not row_id or row_id in seen:
            raise ValueError(f"row {i}: missing or duplicate row_id")
        seen.add(row_id)
        label = row.get("label")
        if label == "unreviewed":
            unreviewed += 1
            continue
        if label not in LABELS:
            raise ValueError(f"row {i}: label must be one of {sorted(LABELS)} or unreviewed")
        text = row.get("text", "")
        for field in ("drug", "event"):
            span = row.get(field, [])
            if (len(span) != 2 or not isinstance(span[0], int) or not isinstance(span[1], int)
                    or not (0 <= span[0] < span[1] <= len(text))):
                raise ValueError(f"row {i}: invalid {field} offsets")
        exported.append({"text": text, "group": row["group"], "drug": row["drug"],
                         "event": row["event"], "label": label})
        counts[label] += 1
    missing = LABELS - set(counts)
    if not args.allow_incomplete and (unreviewed or missing):
        raise ValueError(f"Not ready: {unreviewed} unreviewed rows; missing classes {sorted(missing)}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in exported),
                           encoding="utf-8")
    print(json.dumps({"exported": len(exported), "unreviewed": unreviewed,
                      "label_counts": counts, "missing_classes": sorted(missing),
                      "output": str(args.output)}, indent=2))


if __name__ == "__main__":
    main()
