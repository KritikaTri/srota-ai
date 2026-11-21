"""Fine-tune token classification for drug and adverse-event spans.

JSONL: {"text":"...", "group":"document-id", "entities":[
  {"start": 0, "end": 7, "label":"DRUG"}, ...]}
Offsets are zero-based Python Unicode character offsets; labels are DRUG/EVENT.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from sklearn.model_selection import GroupShuffleSplit

LABELS = ["O", "B-DRUG", "I-DRUG", "B-EVENT", "I-EVENT"]
LABEL2ID = {label: i for i, label in enumerate(LABELS)}


def read_rows(path):
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for n, row in enumerate(rows, 1):
        text = row.get("text")
        if not isinstance(text, str) or not text or not row.get("group"):
            raise ValueError(f"row {n}: text and group are required")
        for ent in row.get("entities", []):
            if ent.get("label") not in {"DRUG", "EVENT"}:
                raise ValueError(f"row {n}: entity label must be DRUG or EVENT")
            if not (0 <= ent.get("start", -1) < ent.get("end", 0) <= len(text)):
                raise ValueError(f"row {n}: invalid entity offsets")
            if not text[ent["start"]:ent["end"]].strip():
                raise ValueError(f"row {n}: empty entity span")
    if len(rows) < 30:
        raise ValueError("Need at least 30 annotated documents for a meaningful split")
    return rows


def split_groups(rows, seed=42):
    groups = np.asarray([r["group"] for r in rows], dtype=object)
    indices = np.arange(len(rows))
    for offset in range(100):
        s1 = GroupShuffleSplit(n_splits=1, test_size=.2, random_state=seed + offset)
        train_valid, test = next(s1.split(indices, groups=groups))
        s2 = GroupShuffleSplit(n_splits=1, test_size=.1875, random_state=seed + 1000 + offset)
        train_rel, valid_rel = next(s2.split(train_valid, groups=groups[train_valid]))
        train, valid = train_valid[train_rel], train_valid[valid_rel]
        def has_spans(ix):
            return any(row.get("entities") for row in (rows[i] for i in ix))
        if all(has_spans(ix) for ix in (train, valid, test)):
            return train, valid, test
    raise ValueError("Could not create group-disjoint splits with entity spans in every partition")


def bio_to_spans(ids, offsets):
    spans = set()
    active = None
    for tag_id, (start, end) in zip(ids, offsets):
        tag = LABELS[int(tag_id)]
        if tag == "O" or start == end:
            if active:
                spans.add(tuple(active))
                active = None
            continue
        prefix, kind = tag.split("-", 1)
        if prefix == "B" or not active or active[2] != kind:
            if active:
                spans.add(tuple(active))
            active = [int(start), int(end), kind]
        else:
            active[1] = int(end)
    if active:
        spans.add(tuple(active))
    return spans


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("data", type=Path)
    parser.add_argument("--base-model", default="dmis-lab/biobert-base-cased-v1.2")
    parser.add_argument("--output", type=Path, default=Path("models/ade_ner"))
    parser.add_argument("--epochs", type=int, default=4)
    args = parser.parse_args()
    from torch.utils.data import Dataset
    from transformers import (AutoModelForTokenClassification, AutoTokenizer,
                              DataCollatorForTokenClassification, Trainer,
                              TrainingArguments)

    rows = read_rows(args.data)
    train_ix, valid_ix, test_ix = split_groups(rows)
    tokenizer = AutoTokenizer.from_pretrained(args.base_model, use_fast=True)
    if not tokenizer.is_fast:
        raise ValueError("A fast tokenizer is required to preserve character spans")

    class EncodedRows(Dataset):
        def __init__(self, indices):
            self.rows = [rows[int(i)] for i in indices]
        def __len__(self):
            return len(self.rows)
        def __getitem__(self, index):
            row = self.rows[index]
            enc = tokenizer(row["text"], truncation=True, max_length=256,
                            return_offsets_mapping=True)
            tags = [0] * len(enc["input_ids"])
            for entity in row.get("entities", []):
                start, end = entity["start"], entity["end"]
                covered = [j for j, (a, b) in enumerate(enc["offset_mapping"])
                           if b > a and a < end and b > start]
                if not covered:
                    continue
                tags[covered[0]] = LABEL2ID["B-" + entity["label"]]
                for j in covered[1:]:
                    tags[j] = LABEL2ID["I-" + entity["label"]]
            tags = [-100 if pair == (0, 0) else tag
                    for tag, pair in zip(tags, enc["offset_mapping"])]
            enc.pop("offset_mapping")
            enc["labels"] = tags
            return enc

    model = AutoModelForTokenClassification.from_pretrained(
        args.base_model, num_labels=len(LABELS), id2label=dict(enumerate(LABELS)),
        label2id=LABEL2ID,
    )
    training_args = TrainingArguments(
        output_dir=str(args.output.parent / "ner-checkpoints"),
        learning_rate=3e-5, per_device_train_batch_size=8,
        per_device_eval_batch_size=16, num_train_epochs=args.epochs,
        weight_decay=.01, evaluation_strategy="epoch", save_strategy="epoch",
        load_best_model_at_end=True, metric_for_best_model="eval_loss",
        report_to="none", seed=42,
    )
    trainer = Trainer(model=model, args=training_args,
                      train_dataset=EncodedRows(train_ix),
                      eval_dataset=EncodedRows(valid_ix),
                      data_collator=DataCollatorForTokenClassification(tokenizer))
    trainer.train()
    args.output.mkdir(parents=True, exist_ok=True)
    trainer.save_model(str(args.output))
    tokenizer.save_pretrained(str(args.output))
    test_rows = [rows[int(i)] for i in test_ix]
    test_ds = EncodedRows(test_ix)
    prediction = trainer.predict(test_ds, metric_key_prefix="test")
    predicted_ids = np.argmax(prediction.predictions, axis=-1)
    exact_tp = exact_fp = exact_fn = 0
    for row_index, row in enumerate(test_rows):
        encoding = tokenizer(row["text"], truncation=True, max_length=256,
                             return_offsets_mapping=True)
        offsets = encoding["offset_mapping"]
        pred_spans = bio_to_spans(predicted_ids[row_index][:len(offsets)], offsets)
        gold_spans = {(e["start"], e["end"], e["label"]) for e in row.get("entities", [])}
        exact_tp += len(pred_spans & gold_spans)
        exact_fp += len(pred_spans - gold_spans)
        exact_fn += len(gold_spans - pred_spans)
    precision = exact_tp / max(1, exact_tp + exact_fp)
    recall = exact_tp / max(1, exact_tp + exact_fn)
    span_f1 = 2 * precision * recall / max(1e-12, precision + recall)
    metrics = prediction.metrics
    (args.output / "test_metrics.json").write_text(json.dumps({
        "test_loss": metrics.get("test_loss"), "test_documents": len(test_ix),
        "exact_span_precision": precision, "exact_span_recall": recall,
        "exact_span_f1": span_f1,
        "label_schema": LABELS,
        "note": "Report exact-span precision/recall/F1 before making quality claims.",
    }, indent=2))
    metrics.update({"test_exact_span_precision": precision,
                    "test_exact_span_recall": recall,
                    "test_exact_span_f1": span_f1})
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
