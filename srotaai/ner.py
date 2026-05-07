"""
NER for SrotaAI — dict-first with confidence scores and a pluggable
spaCy back-end (gracefully no-op if scispaCy / IndicBERT aren't installed).

Three entity types are produced:
  - DRUG    (with .normalized = generic, .brand = original)
  - SYMPTOM
  - ADR_EVENT (a SYMPTOM that co-occurs with a drug + ADR trigger)

Each entity has a `confidence` in [0, 1]:
  dict + word-boundary       0.85
  dict + fuzzy variant       0.65
  spaCy/scispaCy span        model.score (if available)

The brand dictionary is the moat: we map Indian OTC brands to a generic
so PRR is computed on canonical names. The dict is conservative — if we
don't know the mapping, we keep the brand as both surface and normalized
and lower the confidence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Iterable, Optional


# ---------------------------------------------------------------------------
# Lexicons
# ---------------------------------------------------------------------------
# Indian brand -> generic. Lower-case keys.
BRAND_TO_GENERIC: dict[str, str] = {
    # paracetamol
    "crocin": "paracetamol", "crocin advance": "paracetamol",
    "dolo": "paracetamol", "dolo 650": "paracetamol",
    "calpol": "paracetamol", "metacin": "paracetamol",
    # ibuprofen / combinations
    "combiflam": "ibuprofen+paracetamol", "brufen": "ibuprofen",
    # aspirin
    "ecosprin": "aspirin", "disprin": "aspirin",
    # telmisartan
    "telma": "telmisartan", "telma 40": "telmisartan",
    # amlodipine
    "amlong": "amlodipine", "amlodac": "amlodipine",
    # metformin
    "glycomet": "metformin", "glucophage": "metformin",
    # ppi
    "pan-d": "pantoprazole+domperidone", "pan d": "pantoprazole+domperidone",
    "pantop": "pantoprazole", "razo": "rabeprazole",
    # antacid
    "digene": "antacid",
    # antihistamine
    "okacet": "cetirizine", "alex": "phenylephrine+cpm",
    # antibiotic
    "augmentin": "amoxicillin+clavulanate", "azithral": "azithromycin",
    # cough syrup (Coldrif, etc.)
    "coldrif": "phenylephrine+cpm+paracetamol",
    "vicks action 500": "paracetamol+phenylephrine+cpm",
    # synthetic demo brands (used by SyntheticDemoConnector / ref-design)
    "dermacult": "dermacult",
    "somnifert-x": "somnifert-x",
    "somnifert x": "somnifert-x",
    # Zantac / ranitidine (historical PV case)
    "zantac": "ranitidine",
    "zantac 150": "ranitidine",
    "zantac 300": "ranitidine",
}

GENERIC_DRUGS: set[str] = {
    "metformin", "atorvastatin", "rosuvastatin", "amlodipine", "metoprolol",
    "omeprazole", "pantoprazole", "rabeprazole", "levothyroxine", "lisinopril",
    "albuterol", "amoxicillin", "ibuprofen", "ranitidine", "paracetamol",
    "acetaminophen",
    "aspirin", "insulin", "warfarin", "montelukast", "telmisartan",
    "cetirizine", "diclofenac", "azithromycin", "ciprofloxacin",
    "phenylephrine", "domperidone", "diethylene glycol",
    # widely-discussed in Indian / Reddit health threads
    "losartan", "olmesartan", "ramipril", "enalapril", "valsartan",
    "clopidogrel", "rosuvastatin", "simvastatin", "fluoxetine", "sertraline",
    "escitalopram", "alprazolam", "clonazepam", "tramadol", "naproxen",
    "metronidazole", "ofloxacin", "doxycycline", "cefixime", "ondansetron",
    "loratadine", "fexofenadine", "levocetirizine", "montelukast",
    "salbutamol", "budesonide", "prednisone", "prednisolone", "dexamethasone",
    "loperamide", "ranitidine", "famotidine", "esomeprazole",
    "ivermectin", "hydroxychloroquine", "remdesivir",
    "sildenafil", "tadalafil", "finasteride", "minoxidil",
    "isotretinoin", "accutane", "tretinoin",
    "ozempic", "semaglutide", "liraglutide",
    # synthetic demo
    "dermacult", "somnifert-x",
}

# Symptoms incl. Indian-English / code-mixed. Lower-case.
SYMPTOMS: set[str] = {
    "nausea", "headache", "dizziness", "rash", "fatigue", "myalgia",
    "muscle pain", "lactic acidosis", "cough", "diarrhea", "diarrhoea",
    "insomnia", "vomiting", "stomach pain", "abdominal pain", "fever",
    "chills", "sweating", "shortness of breath", "breathlessness",
    "swelling", "edema", "tachycardia", "palpitations", "loose motion",
    # additional ADR-ish symptoms widely discussed in posts
    "constipation", "bloating", "gas", "acidity", "heartburn", "indigestion",
    "weight gain", "weight loss", "hair loss", "hair fall", "acne",
    "anxiety", "depression", "mood swings", "irritability", "drowsiness",
    "blurred vision", "dry mouth", "dry skin", "itching", "hives",
    "low blood pressure", "high blood pressure", "hypotension", "hypertension",
    "kidney pain", "liver pain", "joint pain", "back pain", "chest pain",
    "numbness", "tingling", "burning sensation", "muscle cramps", "cramps",
    "depression", "suicidal", "panic attack", "panic attacks",
    "low energy", "weakness", "lethargy",
    # ref-design / pharmacovigilance events
    "angioedema", "pustular rash", "severe insomnia",
    # Zantac / ranitidine historical
    "ndma contamination", "cancer risk", "liver damage",
    # Hinglish / code-mixed
    "pet mein dard", "pet dard", "sar dard", "chakkar", "chakkar aana",
    "ulti", "buhaar", "bukhar", "thakaan", "saans phulna",
}

# Multilingual / colloquial synonyms → canonical English term.
# Lets the user enter "vomiting" once and still match Hinglish "ulti".
SYMPTOM_SYNONYMS: dict[str, list[str]] = {
    "vomiting":     ["ulti", "ulty", "ulta", "vomit", "throw up", "throwing up"],
    "nausea":       ["matli", "nauseated", "queasy", "feeling sick"],
    "headache":     ["sar dard", "sir dard", "sirdard", "head pain", "migraine"],
    "stomach pain": ["pet dard", "pet mein dard", "pet me dard",
                     "abdominal pain", "tummy ache", "tummy pain"],
    "dizziness":    ["chakkar", "chakkar aana", "lightheaded", "lightheadedness",
                     "giddy", "giddiness", "vertigo"],
    "fatigue":      ["thakaan", "thakan", "tired", "exhaustion", "exhausted",
                     "weakness", "kamzori"],
    "fever":        ["bukhar", "buhaar", "high temperature", "pyrexia"],
    "diarrhea":     ["diarrhoea", "loose motion", "loose motions", "dast",
                     "loose stools"],
    "cough":        ["khansi", "coughing"],
    "rash":         ["daane", "skin rash", "hives", "itching", "khujli"],
    "shortness of breath": ["saans phulna", "breathless", "breathlessness",
                            "saans tut na"],
    "swelling":     ["sujan", "edema", "oedema", "puffiness"],
    "palpitations": ["dhadkan tez", "fast heartbeat", "racing heart"],
    "jaundice":     ["piliya", "yellow eyes", "yellow skin", "icterus"],
    "liver":        ["jigar", "hepatic", "liver enzymes elevated", "hepatitis"],
}

# Inverse: any synonym -> canonical term. Used by keyword expansion + NER.
SYNONYM_TO_CANONICAL: dict[str, str] = {
    syn.lower(): canon
    for canon, syns in SYMPTOM_SYNONYMS.items()
    for syn in syns
}
# Also let canonical map to itself.
for _c in list(SYMPTOM_SYNONYMS):
    SYNONYM_TO_CANONICAL.setdefault(_c, _c)
    SYMPTOMS.add(_c)
    for _s in SYMPTOM_SYNONYMS[_c]:
        SYMPTOMS.add(_s.lower())


def expand_keywords(keywords: list[str]) -> list[str]:
    """
    Expand a user-entered keyword list to also include known synonyms
    and brand⇄generic equivalents. Lets the user enter just `vomiting`
    and still match Hinglish `ulti`, or enter just `Crocin` and match
    `paracetamol`.

    Returns a deduplicated list, preserving the user's original entries first.
    """
    out: list[str] = []
    seen: set[str] = set()
    for kw in keywords:
        for variant in _variants_for(kw):
            v = variant.strip()
            if not v:
                continue
            key = v.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(v)
    return out


def _variants_for(kw: str) -> list[str]:
    low = kw.strip().lower()
    if not low:
        return []
    out = [kw]
    # symptom synonyms — both directions
    canon = SYNONYM_TO_CANONICAL.get(low)
    if canon:
        out.append(canon)
        out.extend(SYMPTOM_SYNONYMS.get(canon, []))
    if low in SYMPTOM_SYNONYMS:
        out.extend(SYMPTOM_SYNONYMS[low])
    # brand → generic and back
    if low in BRAND_TO_GENERIC:
        out.append(BRAND_TO_GENERIC[low])
    for brand, generic in BRAND_TO_GENERIC.items():
        if generic.lower() == low:
            out.append(brand)
    return out

# Sentinel phrases that elevate a (drug,symptom) co-occurrence to ADR_EVENT.
ADR_TRIGGERS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE) for p in [
        r"side\s*effect", r"reaction\s*[:\-]?\s*", r"adr\b",
        r"after\s+(?:taking|starting|using|2\s+weeks|a\s+week|\d+\s+days)",
        r"caused\s+(?:me|my|severe|a|liver|him|her|the)", r"made\s+me",
        r"started\s+having", r"started\s+\w+\s+(?:and|but)\s+(?:now|then)",
        r"developed\s+(?:a\s+|new[-\s]?onset\s+|severe\s+|metabolic\s+)?",
        r"ended\s+up\s+with", r"now\s+(?:severe|having|getting|with)",
        r"\bse\s+(?:dard|chakkar|pareshani)\b", r"\blene\s+ke\s+baad\b",
        r"\bke\s+baad\b", r"getting\s+(?:daily|severe|chronic|persistent)?",
        r"switched\s+(?:me\s+)?off", r"had\s+to\s+stop", r"discontinued",
        r"hospitali[sz]ed", r"new[-\s]?onset",
        # PV / regulatory language
        r"recall(?:ed)?", r"contaminat(?:ed|ion)", r"fda\s+(?:warning|alert|finds)",
        r"class\s+action", r"lawsuit", r"pulled\s+off\s+shelves",
        r"cancer\s+risk", r"risk\s+(?:of|from|is)", r"linked\s+(?:to|my)",
        r"suspects?\s+\w+[\-\s]?(?:induced|caused|related)",
        r"worried\s+about", r"scared", r"elevated\s+(?:enzymes|lactate|levels)",
        r"diagnosed\s+(?:with|while)", r"confirmed",
        r"(?:lab|blood)\s+(?:work|test)", r"biopsy",
        r"long[\-\s]?term\s+(?:use|exposure)", r"years?\s+(?:of|on)\b",
        r"poisoned", r"unsafe", r"banned",
    ]
]


@dataclass
class Entity:
    type: str                          # DRUG | SYMPTOM | ADR_EVENT
    text: str                          # surface form
    span: tuple[int, int]
    normalized: Optional[str] = None   # canonical / generic name
    brand: Optional[str] = None        # if extracted from BRAND dict
    confidence: float = 0.85

    def to_dict(self) -> dict:
        d = asdict(self)
        d["span"] = list(self.span)
        return d


@dataclass
class NerResult:
    drugs: list[Entity] = field(default_factory=list)
    symptoms: list[Entity] = field(default_factory=list)
    adr_events: list[Entity] = field(default_factory=list)
    drug_event_pairs: list[tuple[str, str]] = field(default_factory=list)
    method: str = "dict"

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "drugs":      [e.to_dict() for e in self.drugs],
            "symptoms":   [e.to_dict() for e in self.symptoms],
            "adr_events": [e.to_dict() for e in self.adr_events],
            "drug_event_pairs": [list(p) for p in self.drug_event_pairs],
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _word_boundary_finditer(needle: str, hay: str) -> Iterable[re.Match]:
    pat = re.compile(rf"(?<![A-Za-z0-9]){re.escape(needle)}(?![A-Za-z0-9])",
                     re.IGNORECASE)
    return pat.finditer(hay)


def _dedupe_overlapping(entities: list[Entity]) -> list[Entity]:
    """Prefer the longest, then the highest-confidence span."""
    out: list[Entity] = []
    by_end = sorted(entities, key=lambda e: (e.span[0], -(e.span[1] - e.span[0]),
                                             -e.confidence))
    occupied: list[tuple[int, int]] = []
    for e in by_end:
        s, t = e.span
        if any(not (t <= os_ or s >= ot_) for os_, ot_ in occupied):
            continue
        out.append(e)
        occupied.append((s, t))
    return out


# ---------------------------------------------------------------------------
# Core extractors
# ---------------------------------------------------------------------------
def extract_drugs(text: str) -> list[Entity]:
    found: list[Entity] = []

    # 1. Brand names — order longest-first so "dolo 650" beats "dolo".
    brands = sorted(BRAND_TO_GENERIC.keys(), key=len, reverse=True)
    for brand in brands:
        for m in _word_boundary_finditer(brand, text):
            found.append(Entity(
                type="DRUG", text=text[m.start():m.end()],
                span=(m.start(), m.end()),
                normalized=BRAND_TO_GENERIC[brand],
                brand=brand,
                confidence=0.90,
            ))

    # 2. Generics
    for g in GENERIC_DRUGS:
        for m in _word_boundary_finditer(g, text):
            found.append(Entity(
                type="DRUG", text=text[m.start():m.end()],
                span=(m.start(), m.end()),
                normalized=g, brand=None, confidence=0.85,
            ))

    return _dedupe_overlapping(found)


def extract_symptoms(text: str) -> list[Entity]:
    found: list[Entity] = []
    for sym in sorted(SYMPTOMS, key=len, reverse=True):
        for m in _word_boundary_finditer(sym, text):
            found.append(Entity(
                type="SYMPTOM", text=text[m.start():m.end()],
                span=(m.start(), m.end()),
                normalized=sym.lower(), confidence=0.80,
            ))
    return _dedupe_overlapping(found)


def has_adr_trigger(text: str) -> bool:
    return any(p.search(text) for p in ADR_TRIGGERS)


def extract(text: str, use_spacy: bool = False) -> NerResult:
    """Main entry point. `use_spacy=True` will try scispaCy and merge results."""
    drugs = extract_drugs(text)
    symptoms = extract_symptoms(text)
    method = "dict"

    if use_spacy:
        spacy_ents = _try_spacy(text)
        if spacy_ents is not None:
            drugs += [e for e in spacy_ents if e.type == "DRUG"]
            symptoms += [e for e in spacy_ents if e.type == "SYMPTOM"]
            drugs = _dedupe_overlapping(drugs)
            symptoms = _dedupe_overlapping(symptoms)
            method = "dict+spacy"

    adr_events: list[Entity] = []
    pairs: list[tuple[str, str]] = []
    if drugs and symptoms:
        has_trigger = has_adr_trigger(text)
        # Always emit pairs when drug+symptom co-occur in the same record.
        # Only mark as ADR_EVENT (a stronger claim) when an explicit trigger
        # phrase is present. This keeps recall high for signal detection
        # without inflating ADR_EVENT counts.
        if has_trigger:
            for s in symptoms:
                adr_events.append(Entity(
                    type="ADR_EVENT", text=s.text, span=s.span,
                    normalized=s.normalized,
                    confidence=min(0.85, s.confidence + 0.05),
                ))
        for d in drugs:
            for s in symptoms:
                pairs.append((d.normalized or d.text.lower(),
                              s.normalized or s.text.lower()))

    return NerResult(drugs=drugs, symptoms=symptoms,
                     adr_events=adr_events, drug_event_pairs=pairs,
                     method=method)


# ---------------------------------------------------------------------------
# Optional spaCy back-end
# ---------------------------------------------------------------------------
_SPACY_NLP = None
_SPACY_TRIED = False


def _try_spacy(text: str) -> Optional[list[Entity]]:
    global _SPACY_NLP, _SPACY_TRIED
    if _SPACY_TRIED and _SPACY_NLP is None:
        return None
    if _SPACY_NLP is None:
        try:
            import spacy  # type: ignore
            for model_name in ("en_ner_bc5cdr_md", "en_core_sci_sm", "en_core_web_sm"):
                try:
                    _SPACY_NLP = spacy.load(model_name)
                    break
                except OSError:
                    continue
        except ImportError:
            _SPACY_NLP = None
        _SPACY_TRIED = True
        if _SPACY_NLP is None:
            return None

    doc = _SPACY_NLP(text)
    label_to_type = {"CHEMICAL": "DRUG", "DRUG": "DRUG",
                     "DISEASE": "SYMPTOM", "SYMPTOM": "SYMPTOM",
                     "DISORDER": "SYMPTOM"}
    out: list[Entity] = []
    for ent in doc.ents:
        t = label_to_type.get(ent.label_)
        if not t:
            continue
        out.append(Entity(
            type=t, text=ent.text, span=(ent.start_char, ent.end_char),
            normalized=ent.text.lower(), confidence=0.75,
        ))
    return out


# ---------------------------------------------------------------------------
# CLI / smoke test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json
    samples = [
        "Took Crocin 650 for fever, ended up with bad stomach pain and rash.",
        "Patient on metformin developed lactic acidosis after dose increase.",
        "Dolo lene ke baad pet mein dard ho raha hai.",
        "Started atorvastatin last week, now severe myalgia in calves.",
        "Telma 40 + Ecosprin combo getting daily dizziness.",
        "Just took some painkillers and feeling much better.",
    ]
    for s in samples:
        r = extract(s)
        print("\n" + s)
        print(f"  drugs    : {[(e.text, e.normalized, e.confidence) for e in r.drugs]}")
        print(f"  symptoms : {[(e.text, e.normalized, e.confidence) for e in r.symptoms]}")
        print(f"  ADR      : {bool(r.adr_events)}  pairs={r.drug_event_pairs}")
