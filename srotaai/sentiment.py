"""
Lightweight ADR-aware sentiment for SrotaAI.

We don't need general-purpose sentiment — we need a "is this an adverse
report" signal. Negative-leaning lexicon + ADR triggers from `ner.py` give
a calibrated score in [-1, +1] that downstream signal scoring can use as
a tiebreaker between dict-NER false-positives and real reports.

Returns label ∈ {"adverse", "negative", "neutral", "positive"}.

Negation handling: phrases like "no side effects", "without nausea",
"never had a rash" pull the negative term out of the count and drop it
into the positive bucket (the writer is *reassuring*, not reporting harm).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .ner import ADR_TRIGGERS  # reuse the same triggers


NEGATIVE = {
    "bad", "worse", "worst", "terrible", "awful", "horrible", "severe",
    "painful", "pain", "stop", "quit", "discontinued", "switched off",
    "side effect", "side-effect", "side effects", "adverse", "reaction",
    "ulti", "dard", "chakkar", "pareshani", "phulna",
    "swelling", "rash", "vomit", "vomiting", "nausea", "headache",
    "dizziness", "fatigue", "myalgia", "diarrhea", "fever", "bukhar",
    "hospitalized", "hospitalised", "ER", "emergency",
    # PV/safety-specific terms
    "contamination", "contaminated", "recall", "recalled",
    "cancer", "carcinogen", "carcinogenic", "unsafe", "banned",
    "lawsuit", "class action", "pulled off shelves", "warning",
    "liver damage", "kidney damage", "organ damage",
    "poisoned", "toxic", "toxicity", "risk",
}

POSITIVE = {
    "better", "great", "fine", "good", "improved", "recovered",
    "no side effects", "works well", "effective", "relief", "thik",
    "achha", "kam ho gaya",
}

# Words that flip the polarity of the next ~3 tokens.
NEGATORS = ("no", "not", "never", "without", "zero", "nil", "free of",
            "absence of", "denies", "denied")
NEGATION_WINDOW = 3   # tokens after the negator that get flipped


@dataclass
class SentimentResult:
    label: str
    score: float            # -1 (worst) .. +1 (best)
    matched_negative: list[str]
    matched_positive: list[str]
    has_adr_trigger: bool
    negated: list[str] = None  # noqa: RUF013   # negative terms suppressed by a negator


def _is_negated(text_low: str, term_start: int) -> bool:
    """True if `term` at `term_start` falls within NEGATION_WINDOW tokens
    of a preceding negator. Conservative: only looks left."""
    left = text_low[max(0, term_start - 60):term_start]
    tokens = re.findall(r"\b[\w'\-]+\b", left)
    if not tokens:
        return False
    tail = " " + " ".join(tokens[-(NEGATION_WINDOW + 2):]) + " "
    for neg in NEGATORS:
        if f" {neg} " in tail or tail.endswith(f" {neg} "):
            return True
    return False


def _count_terms(text: str, terms, *, apply_negation: bool = False
                 ) -> tuple[list[str], list[str]]:
    """Return (matched, suppressed_by_negation)."""
    matched: list[str] = []
    suppressed: list[str] = []
    low = text.lower()
    for t in terms:
        for m in re.finditer(rf"(?<![a-z]){re.escape(t.lower())}(?![a-z])", low):
            if apply_negation and _is_negated(low, m.start()):
                suppressed.append(t)
            else:
                matched.append(t)
    return matched, suppressed


def analyze(text: str) -> SentimentResult:
    if not text:
        return SentimentResult("neutral", 0.0, [], [], False, [])
    neg, neg_suppressed = _count_terms(text, NEGATIVE, apply_negation=True)
    pos, _ = _count_terms(text, POSITIVE, apply_negation=False)
    # Each suppressed negative becomes a small positive signal — the writer
    # is *reassuring* the reader, not reporting harm.
    pos = pos + neg_suppressed
    trig = any(p.search(text) for p in ADR_TRIGGERS)

    n, p = len(neg), len(pos)
    raw = (p - n) / max(1, p + n)
    score = max(-1.0, min(1.0, raw))
    if trig and n >= 1:
        label = "adverse"
        score = min(score, -0.5)
    elif n > p:
        label = "negative"
    elif p > n:
        label = "positive"
    else:
        label = "neutral"
    return SentimentResult(label, round(score, 3), neg, pos, trig,
                           neg_suppressed)


if __name__ == "__main__":
    samples = [
        "Crocin worked great, fever gone in 2 hours, no side effects.",
        "Started atorvastatin and developed severe myalgia, had to stop.",
        "Just took some painkillers, feeling fine.",
        "Dolo lene ke baad pet mein dard ho raha hai, ulti bhi.",
        "Patient denies any rash or vomiting after the dose.",
        "Without nausea, the trial would have been a success.",
        "I was worried about side effects but had none.",
    ]
    for s in samples:
        r = analyze(s)
        print(f"{r.label:8s} {r.score:+.2f}  trig={r.has_adr_trigger}  "
              f"neg={r.matched_negative} suppressed={r.negated}  | {s}")
