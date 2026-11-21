# ADE ML pipeline

SrotaAI's learned text component is an **ADE report classifier**. It is
separate from dictionary NER, lexicon sentiment, and report-level PRR/ROR
signal detection. It estimates whether a post is an adverse-event report; it
does not infer clinical causality.

## Dataset contract

Keep licensed source corpora outside Git. Convert annotations to UTF-8 JSONL,
one row per post:

```json
{"text":"I started medicine X and developed a rash","label":"ade","group":"source-document-001"}
{"text":"Medicine X is used to treat headaches","label":"not_ade","group":"source-document-002"}
```

Labels are exactly `ade` and `not_ade`. `group` is a source post/document ID;
all rows from the same original document must share it. The trainer fails if
groups cannot be split without overlap and both classes in each partition.
Do not use the repository's tiny demo fixture as an evaluation dataset.

CADECv2 is a suitable patient-forum corpus and is published under CC BY 4.0
([dataset record](https://researchdata.edu.au/cadecv2/3378660/)); cite CSIRO
when redistributing derived annotations. [SMM4H](https://aclanthology.org/events/smm4h-2021/)
provides ADE classification/extraction benchmarks, with access terms that
should be checked for the selected task. These English corpora do not
validate Hindi/Hinglish performance. Maintain a separate, human-labelled
India-language evaluation set before making that claim.

## Train and evaluate

For CADECv2, download the CC BY 4.0 archive from the [CSIRO collection](https://data.csiro.au/collection/csiro:62387),
extract it outside Git, then convert its BRAT `.txt`/`.ann` pairs:

```bash
python -m srotaai.ml.prepare_brat_corpus /path/to/extracted/cadec \
  --output-dir data/ml
```

The converter uses annotated ADR presence as the ADE post label and maps
Drug/ADR/Symptom spans for NER. Review the mapping against the corpus guide
before training or reporting results. Keep the downloaded source and generated
JSONL under the ignored `data/ml/` path.

```bash
python -m srotaai.ml.train_ade_classifier data/ml/ade_posts.jsonl \
  --output models/ade_classifier.joblib
```

The baseline is word + character TF-IDF with class-balanced logistic
regression. It uses group-disjoint train/validation/test splits; chooses the
ADE threshold on validation only; then reports test precision, recall, F1,
and average precision. Save the printed metrics with the experiment record.
Do not tune against the test split.

When `models/ade_classifier.joblib` exists, ingestion loads the model once per
run and stores `ml_ade_probability`, `ml_ade_label`, and `ml_model_version`
per record. `SROTAAI_ADE_MODEL` can point to another local artifact. Project
CSV exports include those fields. Without a trained artifact, fields remain
empty and the existing rules continue to run; the app never invents ML scores.
Run `python -m srotaai.ml.backfill_ade` after installing a trained model to
score already-ingested records; pass `--force` to replace scores from an older
model version.

For span extraction, create a separate JSONL file with exact Unicode offsets:

```json
{"text":"I started Drug A and developed a rash","group":"post-1","entities":[{"start":10,"end":17,"label":"DRUG"},{"start":32,"end":36,"label":"EVENT"}]}
```

Train the lightweight sequence-labeling model first (no neural framework or
large pretrained download is needed):

```bash
pip install scikit-learn-crfsuite
python -m srotaai.ml.train_crf_ner data/ml/ade_spans.jsonl \
  --output models/ade_ner_crf.joblib
```

This trains a character-aware CRF with group-disjoint train/validation/test
splits and reports exact-span metrics. `srotaai.ner.extract()` automatically
merges predictions from this artifact with the drug lexicon and applies the
sentence assertion rules before creating drug/event pairs. The artifact is
ignored by Git; retain the printed metrics with the experiment record.

An optional BioBERT experiment can also be run when PyTorch is installed:

```bash
python -m srotaai.ml.train_token_ner data/ml/ade_spans.jsonl \
  --output models/ade_ner
```

The script fine-tunes a BioBERT token classifier with group-disjoint splits
and saves exact-span test precision/recall/F1. Once the artifact is present,
`srotaai.ner.extract()` merges transformer spans with dictionary hits. Keep
both models' results in separate benchmark rows; do not imply the NER model
has Hindi/Hinglish coverage unless those spans were represented in training
and evaluation data.

For relation assertion, prepare JSONL rows containing exact `drug` and `event`
character spans plus one of `adverse`, `negated`, `speculative`, or
`co_mention`, then train:

```bash
python -m srotaai.ml.train_relation data/ml/ade_relations.jsonl \
  --output models/ade_relation.joblib
```

When that artifact is available, NER keeps a drug–event pair only when the
learned relation classifier predicts `adverse`; without it, the legacy
sentence-level assertion rules remain in effect. Relation labels require
context and should be annotated by people who understand the dataset guidelines.

Use [`annotation/README.md`](annotation/README.md) for the annotation rules,
independent test-set plan, and relation-queue tools. Evaluate the untouched
gold test split with `python -m srotaai.ml.evaluate_gold <gold.jsonl> --split test`;
it reports ADE, exact span, and (when available) relation scores separately.

## Before claiming model quality

- Compare the learned classifier to the existing rules on the exact same
  held-out groups.
- Report class balance and positive-class precision/recall/F1, not accuracy
  alone; ADE datasets are often imbalanced.
- Report English benchmark and India-language results separately.
- Record dataset version, license, split seed, model artifact/version and
  threshold. Avoid calling the system an autonomous diagnosis or causality
  detector.

The first local CADECv2 run is documented in
[`experiment_2026-11-15.md`](experiment_2026-11-15.md). It records class balance,
held-out metrics, and the span-overlap limitation; rerun the trainers after
changing the corpus or preprocessing and update the report from the new output.
