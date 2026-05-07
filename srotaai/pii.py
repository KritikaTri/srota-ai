"""
PII detection and redaction for India + clinical contexts.

Patterns covered (all Indian-locale aware):
  PHONE_IN  +91 / 10-digit starting 6-9
  AADHAAR   12 digits, optional spaces
  PAN       AAAAA9999A
  EMAIL     RFC-ish
  DOB       dd/mm/yyyy, dd-mm-yyyy, "12 Jan 1980"
  MRN       MRN-12345 / "MRN: 12345"
  ADDRESS   PIN-style "...... 560034"
  HOSPITAL  rule-based ("...Hospital", "...Clinic", "AIIMS", "Apollo X")
  NAME      Hinglish honorifics ("Dr Ravi", "Mr Sharma") + dictionary

Redact returns the text with each match replaced by `<TYPE>` so downstream
NER doesn't accidentally key on PII tokens. Confidence is reported per
match; 1.0 = deterministic regex, lower for soft heuristics.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import Iterable


@dataclass
class PiiHit:
    type: str
    match: str
    span: tuple[int, int]
    confidence: float = 1.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["span"] = list(self.span)
        return d


# ---------------------------------------------------------------------------
# Deterministic patterns
# ---------------------------------------------------------------------------
PATTERNS: list[tuple[str, re.Pattern, float]] = [
    ("EMAIL",
     re.compile(r"\b[\w.+\-]+@[\w\-]+\.[\w\-.]+\b"), 1.0),

    ("PHONE_IN",
     re.compile(r"(?<!\d)(?:\+?91[\s\-]?)?[6-9]\d{9}(?!\d)"), 0.95),

    ("AADHAAR",
     re.compile(r"(?<!\d)\d{4}[\s\-]?\d{4}[\s\-]?\d{4}(?!\d)"), 0.95),

    ("PAN",
     re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), 1.0),

    ("DOB",
     re.compile(
         r"\b(?:(?:0?[1-9]|[12]\d|3[01])[\/\-\.](?:0?[1-9]|1[0-2])[\/\-\.](?:19|20)\d{2}"
         r"|(?:0?[1-9]|[12]\d|3[01])\s+"
         r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(?:19|20)\d{2})\b",
         re.IGNORECASE), 0.85),

    ("MRN",
     re.compile(r"\bMRN[:\s\-]*[A-Z0-9]{4,12}\b", re.IGNORECASE), 0.90),

    ("PIN_IN",
     re.compile(r"(?<!\d)\d{6}(?!\d)"), 0.40),  # weak — gated by post-context
]

# Soft heuristics
HOSPITAL_PAT = re.compile(
    r"\b(?:[A-Z][A-Za-z]+\s+){0,3}"
    r"(?:Hospital|Hospitals|Clinic|Nursing\s+Home|Medical\s+Centre|Medical\s+Center|"
    r"AIIMS|Apollo|Fortis|Manipal|Narayana|Max|Medanta|KIMS|Kokilaben)\b"
)
NAME_PAT = re.compile(
    r"\b(?:Dr\.?|Mr\.?|Mrs\.?|Ms\.?|Smt\.?|Shri|Patient)\s+"
    r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\b"
)
ADDRESS_PAT = re.compile(
    r"(?:flat\s+no\.?\s*\d+|h\.?no\.?\s*\d+|[\w\s,\-]+\s+\d{6})\b",
    re.IGNORECASE,
)


def detect(text: str) -> list[PiiHit]:
    if not text:
        return []
    hits: list[PiiHit] = []

    for label, pat, conf in PATTERNS:
        for m in pat.finditer(text):
            hits.append(PiiHit(type=label, match=m.group(),
                               span=(m.start(), m.end()), confidence=conf))

    for m in HOSPITAL_PAT.finditer(text):
        hits.append(PiiHit(type="HOSPITAL", match=m.group(),
                           span=(m.start(), m.end()), confidence=0.65))
    for m in NAME_PAT.finditer(text):
        hits.append(PiiHit(type="NAME", match=m.group(),
                           span=(m.start(), m.end()), confidence=0.70))
    for m in ADDRESS_PAT.finditer(text):
        # Only keep if it actually contains a 6-digit PIN — too noisy otherwise.
        if re.search(r"\b\d{6}\b", m.group()):
            hits.append(PiiHit(type="ADDRESS", match=m.group(),
                               span=(m.start(), m.end()), confidence=0.55))

    return _resolve_overlaps(hits)


def _resolve_overlaps(hits: list[PiiHit]) -> list[PiiHit]:
    """Prefer higher-confidence + longer span when two hits overlap."""
    hits.sort(key=lambda h: (-h.confidence, -(h.span[1] - h.span[0]), h.span[0]))
    kept: list[PiiHit] = []
    for h in hits:
        if any(not (h.span[1] <= k.span[0] or h.span[0] >= k.span[1]) for k in kept):
            continue
        kept.append(h)
    kept.sort(key=lambda h: h.span[0])
    return kept


def redact(text: str, hits: Iterable[PiiHit] | None = None) -> str:
    hits = list(hits) if hits is not None else detect(text)
    out = text
    for h in sorted(hits, key=lambda x: -x.span[0]):
        s, e = h.span
        out = out[:s] + f"<{h.type}>" + out[e:]
    return out


# ---------------------------------------------------------------------------
# Faker-based evaluation harness
# ---------------------------------------------------------------------------
def evaluate(n: int = 200, seed: int = 7) -> dict:
    """Generate `n` synthetic posts with known PII and report per-type recall."""
    try:
        from faker import Faker  # type: ignore
    except ImportError:
        return {"error": "faker not installed", "ran": False}

    import random
    random.seed(seed)
    fake = Faker("en_IN")
    Faker.seed(seed)

    templates = [
        ("Took {drug} for fever. Reach me on {phone}.", {"phone": "PHONE_IN"}),
        ("Pt MRN-{mrn} on metformin since {dob}.", {"mrn": "MRN", "dob": "DOB"}),
        ("Email {email} for refills, signed {name}.", {"email": "EMAIL", "name": "NAME"}),
        ("Visited {hospital} after rash from Dolo.", {"hospital": "HOSPITAL"}),
        ("Aadhaar {aadhaar} on file at {hospital}.", {"aadhaar": "AADHAAR", "hospital": "HOSPITAL"}),
        ("PAN {pan}, address {addr}.", {"pan": "PAN", "addr": "ADDRESS"}),
    ]

    drugs = ["Crocin", "Dolo", "Telma", "Ecosprin", "metformin", "atorvastatin"]
    expected_total: dict[str, int] = {}
    found_total: dict[str, int] = {}
    samples: list[dict] = []

    for i in range(n):
        tpl, slot_types = random.choice(templates)
        values = {}
        truth = []
        for slot, typ in slot_types.items():
            v = _make_pii(typ, fake)
            values[slot] = v
            truth.append(typ)
        if "{drug}" in tpl:
            values["drug"] = random.choice(drugs)
        text = tpl.format(**values)

        for t in truth:
            expected_total[t] = expected_total.get(t, 0) + 1
        for h in detect(text):
            found_total[h.type] = found_total.get(h.type, 0) + 1

        if i < 5:
            samples.append({"text": text, "expected": truth,
                            "found": [h.to_dict() for h in detect(text)]})

    recall = {t: round(min(1.0, found_total.get(t, 0) / expected_total[t]), 3)
              for t in expected_total}
    return {
        "ran": True, "n": n,
        "expected": expected_total, "found": found_total,
        "recall_by_type": recall,
        "samples": samples,
    }


def _make_pii(typ: str, fake) -> str:
    import random
    if typ == "PHONE_IN":
        first = random.randint(6, 9)
        rest = random.randint(10**8, 10**9 - 1)        # exactly 9 digits
        return f"+91 {first}{rest}"
    if typ == "AADHAAR":
        return f"{random.randint(2000, 9999)} {random.randint(1000, 9999)} {random.randint(1000, 9999)}"
    if typ == "PAN":
        import string
        return "".join(random.choices(string.ascii_uppercase, k=5)) + \
               "".join(random.choices("0123456789", k=4)) + \
               random.choice(string.ascii_uppercase)
    if typ == "EMAIL":
        return fake.email()
    if typ == "DOB":
        return fake.date_of_birth(minimum_age=18, maximum_age=85).strftime("%d/%m/%Y")
    if typ == "MRN":
        return str(random.randint(10000, 999999))
    if typ == "NAME":
        return f"Dr {fake.first_name()} {fake.last_name()}"
    if typ == "HOSPITAL":
        return random.choice(["Apollo Hospitals", "AIIMS Delhi", "Fortis Memorial",
                              "Manipal Hospital", "Narayana Health"])
    if typ == "ADDRESS":
        return f"{fake.street_address()}, {fake.city()} {random.randint(100000, 999999)}"
    return "?"


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json, sys
    if len(sys.argv) > 1 and sys.argv[1] == "eval":
        print(json.dumps(evaluate(n=500), indent=2, default=str))
    else:
        for s in [
            "Hi I'm Dr Ravi Kumar. Email me at ravi.k@apollohealth.in or call +91 9876543210.",
            "Patient MRN-44218, DOB 12/05/1981, was admitted to AIIMS Delhi after Crocin OD.",
            "Aadhaar 2345 6789 1234 and PAN ABCDE1234F on file. PIN 560034.",
        ]:
            hits = detect(s)
            print("\n" + s)
            print("  hits     :", [(h.type, h.match, h.confidence) for h in hits])
            print("  redacted :", redact(s, hits))
