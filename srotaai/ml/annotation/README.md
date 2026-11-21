# Human annotation guide

Use this schema for posts collected with permission and stripped of direct
identifiers. Keep the original source ID as `group`, but store any ID-to-user
mapping outside this repository. Offsets are zero-based Python Unicode
character offsets into the exact `text` string. Do not normalize, trim, or
rewrite text after setting offsets.

## Labels

### Post label (`ade_label`)

- `ade`: the author reports an unwanted symptom/event and explicitly connects
  it to taking or changing a medicine, or reports the event in a clear
  medicine-use temporal context.
- `not_ade`: no reported adverse event. This includes questions about a
  medicine, general use, unrelated symptoms, and posts that explicitly deny
  the event.
- If the post is too ambiguous to decide, exclude it from the benchmark and
  note the reason in the annotation log; do not force a label.

### Entity spans

- `DRUG`: exact mention of a medicine, active ingredient, or brand.
- `EVENT`: exact mention of an unwanted symptom or adverse event. Do not label
  the condition the medicine is intended to treat unless it is itself reported
  as an adverse event.
- Mark each mention separately. If two spans overlap, preserve both in the
  gold JSONL; the flat CRF evaluator will count the exact spans it predicts,
  and its overlap limitation must be reported.

### Relation labels for each same-sentence drug/event candidate

- `adverse`: the post asserts that the event happened in connection with the
  drug. This is a reported association, not proof of causality.
- `negated`: the author denies that the event happened (for example, “no rash
  after taking X”).
- `speculative`: the author only asks or wonders whether the drug could cause
  the event; no event is reported.
- `co_mention`: both are mentioned in the same sentence, but there is no
  stated association. Use this for background/indication mentions too.
- Do not create a relation across sentence boundaries. If relation scope is
  unclear, exclude the post and record the reason rather than guessing.

## Build a useful evaluation set

1. Start with 300–500 eligible posts. Include ADE and non-ADE cases and record
   the sampling method; report metrics for the resulting sample prevalence.
2. Keep the test posts and their labels in a separate file that is not used for
   training, threshold selection, or prompt/rule tuning. Do not use CADECv2
   posts in this independent test set because the current models trained on
   that corpus.
3. Assign all posts from the same source thread/user to one split/group. Keep
   `group` IDs unique across train/validation/test.
4. Have a second annotator independently review at least 20% of the set; resolve
   disagreements before evaluation and set `adjudicated: true` for resolved
   rows.
5. For relation-model training, collect examples of all four relation labels
   across multiple independent groups. The trainer requires every class in
   train, validation, and test. Do not duplicate examples to satisfy this.

## Relation-model queue workflow

The candidate builder creates unlabelled same-sentence pairs from a span JSONL
file. It does not assign labels:

```bash
python -m srotaai.ml.prepare_relation_queue data/ml/ade_spans.jsonl \
  --max-documents 100 --output data/ml/relation_review.jsonl
```

Review each row's `context`, `drug_text`, and `event_text`, then change `label`
from `unreviewed` to one of the four relation labels. The finalizer validates
the queue and writes the trainer format:

```bash
python -m srotaai.ml.finalize_relation_queue data/ml/relation_review.jsonl \
  --output data/ml/ade_relations.jsonl
python -m srotaai.ml.train_relation data/ml/ade_relations.jsonl
```

The strict finalizer requires every candidate to be labelled and all four
classes to occur. Use `--allow-incomplete` only for an intermediate export;
that output is not necessarily trainable.

## Independent evaluation file

Copy `post_template.json` once per post into JSONL (one JSON object per line).
Use `split: "test"` only for the untouched evaluation set. After training the
models on separate data, run:

```bash
python -m srotaai.ml.evaluate_gold data/ml/independent_gold.jsonl --split test
```

The evaluator reports ADE classification and exact entity spans. If a
relation model exists, it also reports relation classification conditional on
gold entity spans, so relation scores do not hide entity-extraction errors.
Report those component scores separately from any end-to-end pair score.
