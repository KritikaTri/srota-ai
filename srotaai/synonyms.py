"""
Synonym expansion — layered approach.

The dict in `ner.py` is fast, deterministic, and auditable, but obviously
doesn't generalise to symptoms we never thought of. This module stacks
three independent layers on top so the user can type *any* symptom and
get reasonable Hinglish / colloquial / clinical variants:

    Layer 1  EXACT DICT       (ner.SYMPTOM_SYNONYMS)        ~ms, deterministic
    Layer 2  FUZZY LEXICAL    (difflib + morphological)     ~ms, no model
    Layer 3  LLM ZERO-SHOT    (OpenAI / local Ollama)       ~1s, optional

Layer 3 is opt-in (env var SROTAAI_LLM=openai|ollama|off). Results from
layer 3 are persisted to `data/synonym_cache.json` so the next run is
free. This mirrors how MedDRA mapping is done in real PV pipelines:
seed dict + a learned model that escalates only when the seed misses.

Why not a sentence-transformer? Cost: ~80MB torch + ~120MB model.
For the demo footprint we keep things lightweight and let the operator
plug in any LLM they already have.
"""
from __future__ import annotations

import difflib
import json
import os
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------------------
# Layer 2 — fuzzy / morphological neighbours
# ---------------------------------------------------------------------------
# Common Hinglish suffix variants & spelling drift patterns.
_HINGLISH_VARIANTS = [
    # vowel drops / doubling
    ("y", "i"), ("i", "y"), ("oo", "u"), ("u", "oo"),
    ("aa", "a"), ("a", "aa"), ("ee", "i"), ("ph", "f"),
    ("z", "j"),
]


def fuzzy_neighbours(term: str, vocabulary: Iterable[str],
                     cutoff: float = 0.82) -> list[str]:
    """Return vocabulary words that look like spelling variants of `term`."""
    term = term.strip().lower()
    if not term:
        return []
    return difflib.get_close_matches(term, list(vocabulary),
                                     n=8, cutoff=cutoff)


def morphological_variants(term: str) -> list[str]:
    """Cheap rule-based variants for Hinglish romanisation drift."""
    out: set[str] = set()
    low = term.lower()
    for a, b in _HINGLISH_VARIANTS:
        if a in low:
            out.add(low.replace(a, b))
    # plural / -ing
    if low.endswith("ing"):
        out.add(low[:-3])
        out.add(low[:-3] + "ed")
    if low.endswith("s") and len(low) > 3:
        out.add(low[:-1])
    return [v for v in out if v != low]


# ---------------------------------------------------------------------------
# Layer 3 — optional LLM
# ---------------------------------------------------------------------------
_CACHE_PATH = Path("data/synonym_cache.json")


def _load_cache() -> dict:
    if _CACHE_PATH.exists():
        try:
            return json.loads(_CACHE_PATH.read_text())
        except Exception:
            return {}
    return {}


def _save_cache(cache: dict) -> None:
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_PATH.write_text(json.dumps(cache, indent=2, ensure_ascii=False))


def _llm_prompt(term: str) -> str:
    return (
        f"List up to 8 Hinglish, Hindi-romanised, colloquial, or "
        f"clinical synonyms for the medical symptom or drug name "
        f"\"{term}\" as it would appear in Indian patient reports on "
        f"WhatsApp/Reddit. Return ONLY a JSON array of strings, no prose."
    )


def llm_expand(term: str, cache: dict | None = None) -> list[str]:
    """Optionally consult an LLM for synonyms. Off by default."""
    backend = os.environ.get("SROTAAI_LLM", "off").lower()
    if backend in ("off", ""):
        return []
    cache = cache if cache is not None else _load_cache()
    key = f"{backend}:{term.lower()}"
    if key in cache:
        return cache[key]

    out: list[str] = []
    try:
        if backend == "openai":
            import openai  # type: ignore
            client = openai.OpenAI()
            r = client.chat.completions.create(
                model=os.environ.get("SROTAAI_LLM_MODEL", "gpt-4o-mini"),
                messages=[{"role": "user", "content": _llm_prompt(term)}],
                temperature=0,
            )
            txt = r.choices[0].message.content or "[]"
            out = json.loads(txt[txt.find("["): txt.rfind("]") + 1])
        elif backend == "ollama":
            import urllib.request
            payload = json.dumps({
                "model": os.environ.get("SROTAAI_LLM_MODEL", "llama3.2"),
                "prompt": _llm_prompt(term),
                "stream": False,
            }).encode()
            req = urllib.request.Request(
                "http://localhost:11434/api/generate",
                data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read())
            txt = body.get("response", "[]")
            out = json.loads(txt[txt.find("["): txt.rfind("]") + 1])
    except Exception as e:
        print(f"[synonyms] LLM backend {backend!r} failed: {e}")
        out = []

    out = [s.strip() for s in out if isinstance(s, str) and s.strip()]
    cache[key] = out
    _save_cache(cache)
    return out


# ---------------------------------------------------------------------------
# Top-level expander used by runner.py
# ---------------------------------------------------------------------------
def expand_keywords_layered(keywords: list[str]) -> tuple[list[str], dict]:
    """
    Returns (expanded_keywords, audit) where audit explains where each
    new term came from. The audit is what powers the "explainability"
    requirement in build.txt Part 2 #5.
    """
    from .ner import expand_keywords as dict_expand, SYMPTOMS

    audit: dict[str, list[dict]] = {}
    seen: set[str] = set()
    out: list[str] = []
    cache = _load_cache()

    for kw in keywords:
        sources: list[dict] = []
        # L1: dict
        l1 = dict_expand([kw])
        for term in l1:
            if term.lower() not in seen:
                seen.add(term.lower())
                out.append(term)
                src = "user" if term.lower() == kw.lower() else "dict"
                sources.append({"term": term, "source": src,
                                "confidence": 1.0 if src == "user" else 0.85})
        # L2: fuzzy + morphological
        for variant in morphological_variants(kw):
            if variant.lower() not in seen and len(variant) > 2:
                seen.add(variant.lower())
                out.append(variant)
                sources.append({"term": variant, "source": "fuzzy_morph",
                                "confidence": 0.55})
        for near in fuzzy_neighbours(kw, SYMPTOMS):
            if near.lower() not in seen:
                seen.add(near.lower())
                out.append(near)
                sources.append({"term": near, "source": "fuzzy_lexical",
                                "confidence": 0.65})
        # L3: LLM (only fires if SROTAAI_LLM is set)
        for term in llm_expand(kw, cache=cache):
            if term.lower() not in seen:
                seen.add(term.lower())
                out.append(term)
                sources.append({"term": term, "source": "llm",
                                "confidence": 0.70})
        audit[kw] = sources

    return out, audit
