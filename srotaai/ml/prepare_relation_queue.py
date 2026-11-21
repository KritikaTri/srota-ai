"""Build a human-review queue of same-sentence DRUG/EVENT candidate pairs.

Input is span JSONL: {text, group, entities:[{start,end,label}]}. The output
uses the relation trainer's format with `label: "unreviewed"` plus a row ID
and context. It contains no inferred relation labels.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


SENTENCE_END = re.compile(r"[.!?;\n]+")


def sentence_bounds(text: str, start: int, end: int):
    left = max((m.end() for m in SENTENCE_END.finditer(text, 0, start)), default=0)
    right_match = SENTENCE_END.search(text, end)
    right = right_match.start() if right_match else len(text)
    return left, right


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("spans", type=Path, help="JSONL with text, group, entities")
    parser.add_argument("--output", type=Path, default=Path("data/ml/relation_review.jsonl"))
    parser.add_argument("--max-documents", type=int, default=0,
                        help="Optional deterministic document cap for a small review batch")
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.spans.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    if args.max_documents:
        rows = rows[:args.max_documents]
    queue = []
    for row in rows:
        text, group = row.get("text", ""), row.get("group")
        if not text or not group:
            raise ValueError("Each span row needs non-empty text and group")
        entities = row.get("entities", [])
        drugs = [e for e in entities if e.get("label") == "DRUG"]
        events = [e for e in entities if e.get("label") == "EVENT"]
        pairs = set()
        for drug in drugs:
            for event in events:
                if drug["start"] == event["start"] and drug["end"] == event["end"]:
                    continue
                bounds = sentence_bounds(text, min(drug["start"], event["start"]),
                                         max(drug["end"], event["end"]))
                if bounds != sentence_bounds(text, event["start"], event["end"]):
                    continue
                if not (bounds[0] <= drug["start"] < drug["end"] <= bounds[1]
                        and bounds[0] <= event["start"] < event["end"] <= bounds[1]):
                    continue
                pairs.add((tuple(drug[k] for k in ("start", "end")),
                           tuple(event[k] for k in ("start", "end")), bounds))
        for drug_span, event_span, bounds in sorted(pairs):
            queue.append({
                "row_id": f"{group}:{drug_span[0]}-{drug_span[1]}:{event_span[0]}-{event_span[1]}",
                "group": group, "text": text,
                "drug": list(drug_span), "event": list(event_span),
                "drug_text": text[drug_span[0]:drug_span[1]],
                "event_text": text[event_span[0]:event_span[1]],
                "context": text[bounds[0]:bounds[1]],
                "label": "unreviewed",
            })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in queue),
                           encoding="utf-8")
    print(f"Wrote {len(queue)} unlabeled candidate pairs from {len(rows)} documents: {args.output}")


if __name__ == "__main__":
    main()
