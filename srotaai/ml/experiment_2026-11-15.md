# SrotaAI ADE / entity baseline (2026-11-15)

## Data and split

- Source: CADECv2, 3,548 BRAT posts. The source is published by CSIRO under
  CC BY 4.0; retain attribution if redistributing derived data.
- ADE target: a post is positive when it has at least one ADR annotation.
  This produces 3,322 positive posts (93.6%) and 226 negative posts.
- Group-disjoint split, seed 42: 2,305 train, 533 validation, 710 held-out
  test documents. Test includes 671 ADE and 39 non-ADE posts.
- The downloaded archive, converted JSONL, and `.joblib` artifacts are local
  ignored files (`data/ml/`, `models/`); they are not included in Git.

## ADE report classifier

Word and character TF-IDF with class-balanced logistic regression. Threshold
0.5661 was selected on validation data, then evaluated once on held-out test:

| Class / metric | Precision | Recall | F1 | Support |
|---|---:|---:|---:|---:|
| ADE | 0.977 | 0.970 | 0.974 | 671 |
| Not ADE | 0.545 | 0.615 | 0.578 | 39 |
| Accuracy |  |  | 0.951 | 710 |

Average precision for ADE was 0.995. Accuracy and ADE scores are inflated by
the source corpus's positive-heavy composition; the negative-class estimate
is especially uncertain because the held-out split has only 39 negatives.
The repository's prior rules baseline scored ADE F1 0.006 on this same
derived label set, so it is not a meaningful standalone comparator for this
dataset. Results are an English benchmark experiment, not clinical validation
or evidence of Hindi/Hinglish performance.

## Drug / event span tagger

A character-aware CRF was trained over BRAT-derived DRUG/EVENT labels, using
the same group-disjoint 2,305/533/710 document split. Exact micro span metrics:

| Split | Precision | Recall | F1 | Gold spans |
|---|---:|---:|---:|---:|
| Validation | 0.781 | 0.726 | 0.753 | 4,867 |
| Held-out test | 0.786 | 0.723 | 0.754 | 6,697 |

Flat BIO tagging cannot encode nested spans. The trainer deterministically
keeps the longest overlapping span and excluded 1,958 overlapping or
unaligned annotations from its encoded targets. Exact span metrics are against
the representable flat targets, not all original BRAT annotations.

## App integration and limits

The trained ADE bundle is loaded by ingestion and stores probability, label,
and model version on each new record. The CRF is merged with dictionary hits
in `srotaai.ner.extract()`; existing sentence-level assertion rules still
reject negated/speculative and unrelated cross-sentence pairs. Smoke checks
confirmed model output persisted to SQLite and the existing Crocin/rash and
Hinglish rule paths still behave as expected.

No relation classifier was trained: CADEC entity annotations do not provide
the required drug–event assertion classes. Relation extraction therefore
remains rule-based. The model does not establish causality, diagnose patients,
or replace pharmacovigilance review. Build a larger, balanced, India-language
and relation-annotated evaluation set before making deployment-quality claims.
