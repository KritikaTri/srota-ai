"""
Runner — execute a Project end-to-end.

Reads a Project YAML, registers it in the store, builds connectors via the
spike1 REGISTRY, fetches records using the project's time window (advanced
to the per-source watermark when available), filters by keywords, and
writes through to SQLite (`srotaai.store.Store`). External-id and
content-hash UNIQUE constraints make a second run idempotent.

Run:
    .venv/bin/python -m srotaai.runner projects/pv-india-otc.yaml
"""
from __future__ import annotations
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make `spikes/` importable so we reuse the connector REGISTRY rather than duplicating it.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "spikes"))

from .project import Project, SourceConfig          # noqa: E402
from .store import Store, DEFAULT_DB_PATH           # noqa: E402
from . import connectors_extra                       # noqa: F401,E402  (registers html_stealth, x_stub)
from . import pii as _pii                            # noqa: E402

# Map Project.connector strings to spike1 REGISTRY keys.
CONNECTOR_ALIAS = {
    "reddit": "reddit",
    "rss": "rss",
    "faers": "faers",
    "openfda": "openfda",
    "whatsapp": "whatsapp",
    "wa": "whatsapp",
    "html": "html_auto",
    "html_auto": "html_auto",
    "html_listing": "html_listing",
    "crawler": "html_listing",
    "generic_html": "generic_html",
    "html_stealth": "html_stealth",
    "x": "x_stub",
    "x_stub": "x_stub",
    "twitter": "x_stub",
    "synthetic_demo": "synthetic_demo",
    "synthetic": "synthetic_demo",
}


def _build_connector(src: SourceConfig):
    from spike1_connectors import REGISTRY  # noqa: WPS433
    key = CONNECTOR_ALIAS.get(src.connector)
    if key is None or key not in REGISTRY:
        raise ValueError(f"unknown connector type: {src.connector}")

    cfg: dict = dict(src.params)
    if src.connector == "reddit":
        # Project allows "subreddits" (list); RedditPlusConnector accepts either.
        # Pass the full list through so the connector can iterate all subs.
        subs = cfg.pop("subreddits", None)
        if subs:
            cfg["subreddits"] = subs
        elif "subreddit" not in cfg:
            cfg["subreddit"] = "all"
        cfg.setdefault("limit", 100)
    return REGISTRY[key](cfg)


def _matches_keywords(text: str, keywords: list[str]) -> list[str]:
    """Case-insensitive substring match. Returns the list of keywords hit."""
    if not text:
        return []
    low = text.lower()
    return [k for k in keywords if k.lower() in low]


def run_project(
    project: Project,
    output_path: Path | None = None,
    db_path: Path | None = None,
) -> dict:
    started = datetime.now(timezone.utc)
    print(f"\n=== Running project: {project.name} ({project.id}) ===")
    # Expand user keywords through three layers: dict → fuzzy/morph → LLM
    # (LLM only if SROTAAI_LLM env var set). Audit trail kept for the
    # build.txt Part-2 explainability requirement.
    from .synonyms import expand_keywords_layered
    project.keywords, kw_audit = expand_keywords_layered(project.keywords)
    print(f"keywords (expanded {len(project.keywords)}): {project.keywords[:20]}"
          + ("..." if len(project.keywords) > 20 else ""))

    store = Store(db_path or DEFAULT_DB_PATH)
    store.upsert_project(project)
    store.append_audit("runner", "run.start", target=project.id,
                       payload={"started_at": started.isoformat(),
                                "keyword_expansion": kw_audit})

    summary = {
        "project_id": project.id,
        "started_at": started.isoformat(),
        "db_path": str(store.path),
        "sources": [],
        "totals": {"fetched": 0, "matched": 0, "inserted": 0, "duplicates": 0},
    }
    all_matched: list[dict] = []

    for src in project.sources:
        if not src.enabled:
            print(f"\n  [skip] {src.name} (disabled)")
            summary["sources"].append({"name": src.name, "skipped": True})
            continue

        source_id = store.upsert_source(project.id, src)

        windows = project.effective_windows(src)
        src_totals = {"fetched": 0, "matched": 0, "inserted": 0, "duplicates": 0}
        per_window: list[dict] = []
        last_wm_after = None

        for win_idx, window in enumerate(windows):
            since, until = window.resolve()

            # Advance `since` to the watermark when available — never refetch
            # older than the configured window though. Watermarks are only
            # meaningful for "live" lookback windows; for absolute ranges the
            # user explicitly set since/until, so respect them.
            wm = store.get_watermark(source_id) if window.lookback_days else None
            effective_since = since
            if wm is not None and wm > since:
                effective_since = wm

            wm_note = f" wm={wm.date()}" if wm else ""
            tag = f" win[{win_idx + 1}/{len(windows)}]" if len(windows) > 1 else ""
            print(f"\n  [{src.name}]{tag} connector={src.connector} latency={src.latency.value} "
                  f"window=[{effective_since.date()} → {until.date()}]{wm_note}")

            matched: list[dict] = []
            try:
                conn = _build_connector(src)

                # Connectors that already produce curated data (no need to
                # re-filter for relevance — the connector itself is the filter).
                _TRUSTED_CONNECTORS = {"openfda", "faers", "rss", "whatsapp"}
                _is_trusted = src.connector in _TRUSTED_CONNECTORS

                def _stream(_since=effective_since, _until=until):
                    from . import ner as _ner_mod
                    for rec in conn.fetch(since=_since):
                        if rec.posted_at and rec.posted_at > _until:
                            continue
                        if rec.posted_at and rec.posted_at < _since:
                            continue
                        hits = _matches_keywords(rec.text, project.keywords)
                        # Relevance gate: keep records that either match a
                        # project keyword OR contain at least one known drug
                        # or symptom. Otherwise drop — prevents general-
                        # subreddit noise (politics/weather) from drowning
                        # NER coverage and inflating the d-cell of 2x2.
                        # Trusted connectors (openFDA, FAERS, curated RSS)
                        # bypass this — their feeds are already curated.
                        if not _is_trusted and not hits:
                            try:
                                _ner_quick = _ner_mod.extract(rec.text or "")
                            except Exception:
                                _ner_quick = None
                            if not _ner_quick or (
                                not _ner_quick.drugs
                                and not _ner_quick.symptoms
                            ):
                                continue
                        if hits:
                            matched.append({
                                "external_id": rec.external_id,
                                "url": rec.url,
                                "posted_at": rec.posted_at.isoformat() if rec.posted_at else None,
                                "matched_keywords": hits,
                                "preview": (rec.text or "")[:160].replace("\n", " "),
                                "source": src.name,
                            })
                        redacted = _pii.redact(rec.text or "")
                        yield rec, hits, redacted

                r = store.insert_records(source_id, _stream())
            except Exception as e:                              # noqa: BLE001
                print(f"    ERROR: {type(e).__name__}: {e}")
                store.append_audit("runner", "source.error", target=src.name,
                                   payload={"error": repr(e), "window_index": win_idx})
                per_window.append({"window_index": win_idx, "error": repr(e),
                                   "since": effective_since.isoformat(),
                                   "until": until.isoformat()})
                continue

            print(f"    fetched={r.fetched}  matched={len(matched)}  "
                  f"inserted={r.inserted}  duplicates={r.duplicates}")
            for m in matched[:3]:
                print(f"      - [{','.join(m['matched_keywords'])}] {m['preview']}")
            if len(matched) > 3:
                print(f"      ... ({len(matched) - 3} more)")

            per_window.append({
                "window_index": win_idx,
                "since": effective_since.isoformat(), "until": until.isoformat(),
                "fetched": r.fetched, "matched": len(matched),
                "inserted": r.inserted, "duplicates": r.duplicates,
                "watermark_before": wm.isoformat() if wm else None,
                "watermark_after": r.last_seen_ts.isoformat() if r.last_seen_ts else None,
            })
            src_totals["fetched"] += r.fetched
            src_totals["matched"] += len(matched)
            src_totals["inserted"] += r.inserted
            src_totals["duplicates"] += r.duplicates
            if r.last_seen_ts is not None:
                last_wm_after = r.last_seen_ts.isoformat()
            all_matched.extend(matched)

        summary["sources"].append({
            "name": src.name, "connector": src.connector,
            "windows": per_window, **src_totals,
            "watermark_after": last_wm_after,
        })
        summary["totals"]["fetched"]     += src_totals["fetched"]
        summary["totals"]["matched"]     += src_totals["matched"]
        summary["totals"]["inserted"]    += src_totals["inserted"]
        summary["totals"]["duplicates"]  += src_totals["duplicates"]

        store.append_audit("runner", "source.done", target=src.name,
                           payload={**src_totals, "windows": len(windows)})

    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    summary["matched_records"] = all_matched

    store.append_audit("runner", "run.end", target=project.id,
                       payload=summary["totals"])
    store.close()

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(summary, indent=2, default=str))
        print(f"\n[OK] manifest -> {output_path}")

    t = summary["totals"]
    print(f"\n=== Done. fetched={t['fetched']} matched={t['matched']} "
          f"inserted={t['inserted']} duplicates={t['duplicates']} ===")
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project_yaml", help="Path to project YAML")
    ap.add_argument("-o", "--out", default=None, help="Output manifest JSON path")
    ap.add_argument("--db", default=None, help=f"SQLite DB path (default: {DEFAULT_DB_PATH})")
    args = ap.parse_args()

    project = Project.from_yaml(args.project_yaml)
    out = Path(args.out) if args.out else ROOT / "spike_outputs" / f"runner_{project.id}.json"
    db = Path(args.db) if args.db else None
    run_project(project, out, db_path=db)


if __name__ == "__main__":
    main()
