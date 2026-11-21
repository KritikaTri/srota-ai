"""Convert a local BRAT standoff corpus (such as CADECv2) to SrotaAI JSONL.

The source archive is deliberately not bundled. Input must contain matching
``*.txt`` and ``*.ann`` pairs. ADR annotations create ADE-positive labels;
Drug spans map to DRUG and ADR/Symptom spans map to EVENT for NER.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def parse_ann(path: Path, text: str):
    entities = []
    adverse_present = False
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        fields = line.split("\t", 2)
        if len(fields) != 3 or not fields[0].startswith("T"):
            continue
        spec = fields[1].split()
        if len(spec) < 3:
            raise ValueError(f"{path}:{line_no}: malformed text-bound annotation")
        raw_label = spec[0].lower()
        mapped = "DRUG" if raw_label in {"drug", "chemical", "medication"} else None
        is_adr = raw_label in {"adr", "adverse", "adverse_drug_event", "adverse_event"}
        is_event = is_adr or raw_label in {"symptom", "disease", "finding", "event"}
        if not mapped and not is_event:
            continue
        if is_adr:
            adverse_present = True
        label = mapped or "EVENT"
        # BRAT allows discontinuous spans (e.g. 2 5;8 12); preserve each
        # contiguous segment as a separate token-classification target.
        for segment in " ".join(spec[1:]).split(";"):
            start, end = map(int, segment.split())
            if not (0 <= start < end <= len(text)):
                raise ValueError(f"{path}:{line_no}: span is outside the text")
            entities.append({"start": start, "end": end, "label": label})
    return entities, adverse_present


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus", type=Path, help="Extracted BRAT corpus directory")
    ap.add_argument("--output-dir", type=Path, default=Path("data/ml"))
    args = ap.parse_args()
    texts = sorted(args.corpus.rglob("*.txt"))
    if not texts:
        raise SystemExit(f"No .txt documents found under {args.corpus}")
    annotations = {path.stem: path for path in args.corpus.rglob("*.ann")}
    classification = []
    spans = []
    for txt_path in texts:
        ann_path = annotations.get(txt_path.stem)
        if ann_path is None:
            continue
        text = txt_path.read_text(encoding="utf-8")
        entities, has_adr = parse_ann(ann_path, text)
        group = str(txt_path.relative_to(args.corpus).with_suffix(""))
        classification.append({"text": text, "label": "ade" if has_adr else "not_ade", "group": group})
        spans.append({"text": text, "group": group, "entities": entities})
    if not classification:
        raise SystemExit("No matching .txt/.ann pairs found")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for filename, rows in (("ade_posts.jsonl", classification), ("ade_spans.jsonl", spans)):
        path = args.output_dir / filename
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
        print(f"Wrote {len(rows)} rows: {path}")
    positives = sum(row["label"] == "ade" for row in classification)
    print(f"Corpus rows={len(classification)} ADR-positive={positives} ({positives/len(classification):.1%})")


if __name__ == "__main__":
    main()
