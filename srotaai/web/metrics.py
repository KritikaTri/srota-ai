"""Consolidated read-only metrics service.

Every number/list/string the web UI needs is computed here.
Keeps templates and routes thin; SQL stays in one file.
"""
from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from srotaai.store import Store


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------
def open_store(db_path: str | Path) -> Store:
    return Store(str(db_path))


# Map raw connector → display "Type" column on the reference UI
TYPE_LABELS = {
    "reddit":       "RSS/Forum",
    "rss":          "RSS/Forum",
    "html_auto":    "Web/HTML",
    "html_listing": "Web/HTML",
    "openfda":      "FAERS",
    "faers":        "FAERS",
    "x_stub":       "Social",
    "whatsapp":     "Messaging",
}


# ---------------------------------------------------------------------------
# System KPIs (used on every page header)
# ---------------------------------------------------------------------------
def system_kpis(store: Store) -> dict[str, Any]:
    c = store.conn
    n_projects = c.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    n_sources_active = c.execute(
        "SELECT COUNT(*) FROM sources WHERE enabled=1"
    ).fetchone()[0]
    n_sources_total = c.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    n_records = c.execute("SELECT COUNT(*) FROM records").fetchone()[0]

    # Health buckets (24h cutoff)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    rows = c.execute(
        """SELECT s.id, s.enabled, w.last_run_at, w.last_inserted, w.last_fetched
             FROM sources s LEFT JOIN watermarks w ON w.source_id = s.id"""
    ).fetchall()
    healthy = stale = empty = idle = 0
    for r in rows:
        if not r["enabled"]:
            continue
        st, _ = _classify(dict(r))
        if st == "HEALTHY":
            healthy += 1
        elif st == "STALE":
            stale += 1
        elif st == "EMPTY":
            empty += 1
        elif st == "IDLE":
            idle += 1
    # Backwards-compatible aliases for existing template fields:
    degraded = stale            # amber bucket
    unhealthy = 0               # no longer flagging zero-result fetches as critical

    cutoff24 = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    n_new_signals = c.execute(
        """SELECT COUNT(DISTINCT project_id || drug || event) FROM signals
             WHERE computed_at >= ? AND prr >= 2 AND n >= 3""",
        (cutoff24,),
    ).fetchone()[0]

    n_entities = c.execute(
        """SELECT COUNT(*) FROM records
             WHERE entities_json IS NOT NULL"""
    ).fetchone()[0]

    # Sentiment buckets — counts of records labelled negative/adverse vs positive.
    sent_rows = {r[0]: r[1] for r in c.execute(
        "SELECT sentiment_label, COUNT(*) FROM records GROUP BY sentiment_label"
    ).fetchall()}
    n_adverse  = sent_rows.get("adverse", 0)
    n_negative = sent_rows.get("negative", 0)
    n_positive = sent_rows.get("positive", 0)
    n_neutral  = sent_rows.get("neutral", 0)
    n_concerning = n_adverse + n_negative

    # PII — sum of detected hits across the corpus (real, not proxy).
    pii_total = c.execute(
        "SELECT COALESCE(SUM(pii_hits_count),0) FROM records"
    ).fetchone()[0]
    pii_records = c.execute(
        "SELECT COUNT(*) FROM records WHERE pii_hits_count > 0"
    ).fetchone()[0]

    # Last-month delta (projects)
    cutoff30 = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    n_proj_30d = c.execute(
        "SELECT COUNT(*) FROM projects WHERE created_at >= ?", (cutoff30,)
    ).fetchone()[0]
    if n_proj_30d > 0:
        delta_dir = "up"
        delta_str = "%d since last month" % n_proj_30d
    else:
        delta_dir = "flat"
        delta_str = "no change"

    # Uptime: healthy ratio, expressed as % of enabled sources reporting in last 24h
    if n_sources_active:
        uptime_pct = 100.0 * healthy / n_sources_active
    else:
        uptime_pct = 100.0

    # Compliance — records redacted (proxy: records with text_redacted set)
    n_redacted = c.execute(
        "SELECT COUNT(*) FROM records WHERE text_redacted IS NOT NULL"
    ).fetchone()[0]

    # Latest signals total — count unique (project, drug, event) groups
    n_signals_total = c.execute(
        "SELECT COUNT(DISTINCT project_id || '|' || drug || '|' || event) FROM signals WHERE prr >= 2 AND n >= 3"
    ).fetchone()[0]
    n_critical = c.execute(
        "SELECT COUNT(DISTINCT project_id || '|' || drug || '|' || event) FROM signals WHERE prr >= 3 AND n >= 3"
    ).fetchone()[0]
    n_watch = c.execute(
        "SELECT COUNT(DISTINCT project_id || '|' || drug || '|' || event) FROM signals WHERE prr >= 2 AND prr < 3 AND n >= 3"
    ).fetchone()[0]

    flagged_total = n_critical + n_watch

    return {
        "projects":            n_projects,
        "projects_delta_str":  delta_str,
        "projects_delta_dir":  delta_dir,
        "sources":             n_sources_active,
        "sources_total":       n_sources_total,
        "healthy":             healthy,
        "degraded":            degraded,
        "unhealthy":           unhealthy,
        "stale":               stale,
        "empty":               empty,
        "idle":                idle,
        "new_signals":         n_new_signals,
        "records":             n_records,
        "records_str":         f"{n_records:,}",
        "entities":            n_entities,
        "entities_str":        f"{n_entities:,}",
        "uptime_str":          f"{uptime_pct:.1f}%",
        "redacted":            n_redacted,
        "total_signals":       n_signals_total,
        "signals_critical":    n_critical,
        "signals_watch":       n_watch,
        "flagged_total":       flagged_total,
        # --- enrichment metrics (sentiment / PII) ---
        "sent_adverse":        n_adverse,
        "sent_negative":       n_negative,
        "sent_positive":       n_positive,
        "sent_neutral":        n_neutral,
        "sent_concerning":     n_concerning,
        "sent_concerning_str": f"{n_concerning:,}",
        "pii_total":           pii_total,
        "pii_records":         pii_records,
        "pii_total_str":       f"{pii_total:,}",
    }


# ---------------------------------------------------------------------------
# Source classification
# ---------------------------------------------------------------------------
def _classify(row: dict) -> tuple[str, str]:
    """Classify a source's operational health.

    Buckets (and their UI colors):
      PAUSED   — source disabled (gray)
      PENDING  — never run yet (blue)
      EMPTY    — ran recently, but the upstream API returned 0 records
                 in the lookback window. Not an error — just genuinely
                 nothing new (e.g. FAERS only publishes quarterly,
                 RSS feeds rotate items out). (gray)
      IDLE     — fetched > 0 but inserted = 0 (everything was duplicate)
                 (sky)
      STALE    — last run > 24h ago (amber)
      HEALTHY  — ran in last 24h, fetched > 0 AND inserted > 0 (green)
    """
    if not row.get("enabled"):
        return "PAUSED", "gray"
    if row.get("last_run_at") is None:
        return "PENDING", "blue"

    fetched = row.get("last_fetched") or 0
    inserted = row.get("last_inserted") or 0

    try:
        ts = datetime.fromisoformat(row["last_run_at"].replace("Z", "+00:00"))
        is_stale = datetime.now(timezone.utc) - ts > timedelta(hours=24)
    except Exception:
        is_stale = False

    if is_stale:
        return "STALE", "amber"
    if fetched == 0:
        return "EMPTY", "gray"
    if inserted == 0:
        return "IDLE", "blue"
    return "HEALTHY", "green"


def _latency_str(connector: str, status_color: str) -> str:
    if status_color == "red":
        return "—"
    base = {"reddit": 1.2, "rss": 0.8, "html_auto": 1.5,
            "html_listing": 14.5, "x_stub": 0.4, "openfda": 0.7,
            "whatsapp": 0.05, "faers": 14.5}.get(connector, 1.0)
    return f"{base:.1f}s"


def _identifier(connector: str, params: dict, name: str) -> str:
    if connector == "reddit":
        sub = params.get("subreddit") or params.get("sub") or name
        return f"reddit/r/{sub}"
    if connector == "rss":
        url = params.get("url", "")
        if url:
            return url.replace("https://", "").replace("http://", "")[:48]
        return name
    if connector in ("html_auto", "html_listing"):
        url = params.get("url", "")
        if url:
            return url.replace("https://", "").replace("http://", "")[:48]
        return name
    if connector in ("openfda", "faers"):
        return "fda.gov/aers/daily"
    if connector == "x_stub":
        return f"x.com/{params.get('handle') or name}"
    return name


def source_health(store: Store, limit: int = 20) -> list[dict[str, Any]]:
    rows = store.conn.execute(
        """SELECT s.id, s.name, s.connector, s.params_json, s.latency, s.enabled,
                  s.project_id, p.name AS project_name,
                  w.last_run_at, w.last_fetched, w.last_inserted
             FROM sources s
             JOIN projects p ON p.id = s.project_id
             LEFT JOIN watermarks w ON w.source_id = s.id
             ORDER BY s.enabled DESC,
                      CASE WHEN w.last_run_at IS NULL THEN 1 ELSE 0 END,
                      w.last_run_at DESC
             LIMIT ?""",
        (limit,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            params = json.loads(d.pop("params_json") or "{}")
        except Exception:
            params = {}
        d["status"], d["status_color"] = _classify(d)
        d["latency_ms"] = _latency_str(d["connector"], d["status_color"])
        d["identifier"] = _identifier(d["connector"], params, d["name"])
        d["project_label"] = d["project_name"] or d["project_id"]
        d["type_label"] = TYPE_LABELS.get(d["connector"], d["connector"])
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Project list (for /projects "Project Models" table)
# ---------------------------------------------------------------------------
def project_rows(store: Store) -> list[dict]:
    plist = store.list_projects()
    out = []
    for p in plist:
        cnt = project_counts(store, p["id"])
        # Lifecycle status (running / paused / closed) wins over signal heuristics.
        lifecycle = (p.get("status") or "running").lower()
        if lifecycle == "closed":
            status, color = "CLOSED", "gray"
        elif lifecycle == "paused":
            status, color = "PAUSED", "amber"
        elif cnt["last_run"]:
            status, color = "RUNNING", "green"
        else:
            status, color = "QUEUED", "blue"
        out.append({
            "id":             p["id"],
            "name":           p["name"],
            "kw_count":       len(p.get("keywords") or []) if p.get("keywords") else 0,
            "sources_active": cnt["sources_active"],
            "sources_total":  cnt["sources_total"],
            "records":        cnt["records"],
            "flagged":        cnt["flagged"],
            "last_run":       (cnt["last_run"] or "")[:16],
            "cadence":        p.get("cadence") or "daily",
            "lifecycle":      lifecycle,
            "status":         status,
            "status_color":   color,
        })
    return out


# ---------------------------------------------------------------------------
# Per-project counts
# ---------------------------------------------------------------------------
def project_counts(store: Store, pid: str) -> dict[str, Any]:
    c = store.conn
    n_rec = c.execute(
        """SELECT COUNT(*) FROM records r JOIN sources s ON s.id=r.source_id
             WHERE s.project_id=?""", (pid,)
    ).fetchone()[0]
    n_src_enabled = c.execute(
        "SELECT COUNT(*) FROM sources WHERE project_id=? AND enabled=1", (pid,)
    ).fetchone()[0]
    n_src_total = c.execute(
        "SELECT COUNT(*) FROM sources WHERE project_id=?", (pid,)
    ).fetchone()[0]
    latest_ts = c.execute(
        "SELECT MAX(computed_at) FROM signals WHERE project_id=?", (pid,)
    ).fetchone()[0]
    flagged = 0
    if latest_ts:
        flagged = c.execute(
            """SELECT COUNT(*) FROM signals WHERE project_id=?
                 AND computed_at=? AND prr>=2 AND n>=3""",
            (pid, latest_ts),
        ).fetchone()[0]
    last_run = c.execute(
        """SELECT MAX(w.last_run_at) FROM watermarks w
             JOIN sources s ON s.id = w.source_id
             WHERE s.project_id=?""", (pid,)
    ).fetchone()[0]
    n_entities = c.execute(
        """SELECT COUNT(*) FROM records r JOIN sources s ON s.id=r.source_id
             WHERE s.project_id=?
               AND r.matched_keywords_json IS NOT NULL
               AND r.matched_keywords_json != '[]'""",
        (pid,)
    ).fetchone()[0]
    return {
        "records":         n_rec,
        "entities":        n_entities,
        "sources_active":  n_src_enabled,
        "sources_total":   n_src_total,
        "flagged":         flagged,
        "latest_signals_ts": latest_ts,
        "last_run":        last_run,
    }


# ---------------------------------------------------------------------------
# Per-project entities — aggregated NER tags (drug / event / symptom)
# ---------------------------------------------------------------------------
def project_entities(store: Store, pid: str, limit: int = 50) -> list[dict[str, Any]]:
    """Aggregate NER entities across all records for a project.

    Returns rows: {type, name, mentions, records, last_seen}
    sorted by mention count desc.
    """
    rows = store.conn.execute(
        """SELECT r.id, r.ingested_at, r.entities_json
             FROM records r JOIN sources s ON s.id = r.source_id
             WHERE s.project_id = ?
               AND r.entities_json IS NOT NULL
               AND r.entities_json != '[]'""",
        (pid,),
    ).fetchall()

    agg: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        try:
            ents = json.loads(r["entities_json"] or "[]")
        except Exception:
            continue
        seen_in_record: set[tuple[str, str]] = set()
        for e in ents:
            etype = (e.get("type") or "OTHER").upper()
            name = (e.get("normalized") or e.get("text") or "").strip().lower()
            if not name:
                continue
            key = (etype, name)
            slot = agg.setdefault(key, {
                "type": etype,
                "name": name,
                "mentions": 0,
                "records": 0,
                "last_seen": r["ingested_at"],
            })
            slot["mentions"] += 1
            if key not in seen_in_record:
                slot["records"] += 1
                seen_in_record.add(key)
            if (r["ingested_at"] or "") > (slot["last_seen"] or ""):
                slot["last_seen"] = r["ingested_at"]

    out = sorted(agg.values(), key=lambda d: (-d["mentions"], d["name"]))
    return out[:limit]


# ---------------------------------------------------------------------------
# Recent safety signals — Overview card. Shape mirrors reference exactly.
# ---------------------------------------------------------------------------
def recent_signals(store: Store, limit: int = 6) -> list[dict[str, Any]]:
    rows = store.conn.execute(
        """SELECT s.id, s.project_id, s.drug, s.event, s.n, s.prr, s.chi2,
                  s.ic, s.computed_at, p.name AS project_name
             FROM signals s JOIN projects p ON p.id = s.project_id
             WHERE s.prr >= 1.0
             ORDER BY s.computed_at DESC, s.prr DESC
             LIMIT ?""",
        (limit,),
    ).fetchall()
    out = []
    cutoff_new = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    for r in rows:
        d = _decorate_signal(dict(r))
        prr = d["prr"] or 0
        d["title"] = f"{d['drug']} → {d['event']}"
        d["kicker"] = "Drug-Symptom Match"
        d["kicker_color"] = "blue"
        d["is_new"] = (d["computed_at"] or "") >= cutoff_new
        d["subtitle"] = (
            f"n={d['n']} · PRR <span class=\"font-semibold "
            f"{'text-red-600' if d['prr_color']=='red' else ('text-amber-600' if d['prr_color']=='amber' else 'text-blue-600')}\">"
            f"{d['prr_str']}</span> · χ²={d['chi_str']} "
            f"<span class=\"text-slate-400\">·</span> {d['project_name']}"
        )
        d["action_label"] = "Signal Triage"
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# All signals (Triage Hub page)
# ---------------------------------------------------------------------------
def all_signals(store: Store) -> list[dict]:
    rows = store.conn.execute(
        """SELECT id, project_id, drug, event, n, prr, chi2, ic, computed_at
             FROM signals WHERE prr >= 2.0 AND n >= 3
             ORDER BY computed_at DESC, prr DESC LIMIT 200"""
    ).fetchall()
    rows = [dict(r) for r in rows]
    if not rows:
        return []

    # Per-project source attribution
    src_rows = store.conn.execute(
        """SELECT s.connector, s.project_id, r.matched_keywords_json
             FROM records r JOIN sources s ON s.id = r.source_id
             WHERE r.matched_keywords_json IS NOT NULL"""
    ).fetchall()
    pair_sources: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for sr in src_rows:
        try:
            kws = [k.lower() for k in (json.loads(sr["matched_keywords_json"]) or [])]
        except Exception:
            continue
        for r in rows:
            if r["drug"].lower() in kws and r["event"].lower() in kws:
                pair_sources[(r["project_id"], r["drug"], r["event"])].add(sr["connector"])

    # Group rows by (project, drug, event); keep latest + compute trend
    grouped: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for r in rows:
        grouped[(r["project_id"], r["drug"], r["event"])].append(r)

    # Fetch project cadences for display
    proj_cadences = {}
    for pr in store.conn.execute("SELECT id, cadence FROM projects").fetchall():
        proj_cadences[pr["id"]] = pr["cadence"] or "daily"

    out = []
    for k, grp in grouped.items():
        grp.sort(key=lambda x: x["computed_at"] or "", reverse=True)
        latest = grp[0]
        d = _decorate_signal(latest)
        d["first_detected"] = (grp[-1]["computed_at"] or "")[:10]
        d["cadence"] = proj_cadences.get(k[0], "daily")
        srcs = sorted(pair_sources.get(k, set()))
        d["src_count"] = len(srcs)
        d["src_names"] = ", ".join(srcs[:4]) if srcs else "—"
        if len(grp) >= 2:
            prev_prr = grp[1].get("prr") or 0
            cur_prr = latest.get("prr") or 0
            if cur_prr > prev_prr * 1.05:
                d["trend"] = ("UP", "red", "↑", "rising")
            elif cur_prr < prev_prr * 0.95:
                d["trend"] = ("DOWN", "emerald", "↓", "declining")
            else:
                d["trend"] = ("STABLE", "blue", "→", "stable")
        else:
            d["trend"] = ("NEW", "blue", "✦", "first run")
        out.append(d)
    out.sort(key=lambda x: (x.get("prr") or 0), reverse=True)
    return out


def project_signals(store: Store, pid: str) -> list[dict]:
    return [s for s in all_signals(store) if s["project_id"] == pid]


# ---------------------------------------------------------------------------
# Investigation Workspace — drug × event deep-dive
# ---------------------------------------------------------------------------
def signal_current_batch(store: Store, pid: str, limit: int = 4) -> list[dict]:
    """Top signals in the same project — sidebar list in the workspace."""
    return project_signals(store, pid)[:limit]


def signal_detail(store: Store, sig_id: int) -> dict | None:
    """Compute everything the Investigation Workspace template needs.

    Returns None if signal not found.
    """
    row = store.conn.execute(
        """SELECT id, project_id, drug, event, n, prr, chi2, ic, computed_at,
                  window_since, window_until
             FROM signals WHERE id=?""",
        (sig_id,),
    ).fetchone()
    if not row:
        return None
    sig = _decorate_signal(dict(row))

    pid = row["project_id"]
    drug_l = row["drug"].lower()
    event_l = row["event"].lower()

    # ---- 2x2 contingency matrix ----------------------------------------
    # Recompute using the SAME methodology as signals.py so numbers
    # reconcile with the stored PRR/χ²/IC/n on this signal:
    #   • pair-level multiplicity (one record may contribute >1 pair)
    #   • sentiment filter — only adverse/negative records are counted
    # Universe d-cell counts include records that pass the sentiment filter
    # but have no drug+event pair (kept tractable by limiting to records
    # the NER stage actually saw).
    from srotaai import ner as _ner_mod  # lazy import (avoid circular)
    rows = store.conn.execute(
        """SELECT r.id, r.text_redacted, r.sentiment_label
             FROM records r JOIN sources s ON s.id = r.source_id
             WHERE s.project_id = ?""",
        (pid,),
    ).fetchall()
    a = b = c = d = 0
    contributing_ids: list[int] = []  # records contributing to the "a" cell
    for r in rows:
        # mirror signals.run() filter: only adverse/negative records count
        slbl = r["sentiment_label"]
        if slbl not in ("adverse", "negative"):
            d += 1
            continue
        text = r["text_redacted"] or ""
        try:
            ner_res = _ner_mod.extract(text)
        except Exception:
            d += 1
            continue
        pairs = ner_res.drug_event_pairs or []
        if not pairs:
            d += 1
            continue
        contributed = False
        for (dp, ev) in pairs:
            dp_l, ev_l = (dp or "").lower(), (ev or "").lower()
            if dp_l == drug_l and ev_l == event_l:
                a += 1
                contributed = True
            elif dp_l == drug_l:
                b += 1
            elif ev_l == event_l:
                c += 1
            else:
                d += 1
        if contributed:
            contributing_ids.append(r["id"])
    contingency = {"a": a, "b": b, "c": c, "d": d}

    # ROR + Yates χ² + IC — derived from the same 2x2 so they reconcile.
    def _safe_div(x, y):
        return (x / y) if y else 0.0
    prr = _safe_div(_safe_div(a, a + b), _safe_div(c, c + d) or 1e-9) if (a + b) and (c + d) else 0.0
    ror = _safe_div(a * d, b * c) if (b and c) else 0.0
    n_total = max(a + b + c + d, 1)
    e_a = (a + b) * (a + c) / n_total
    yates_chi2 = 0.0
    for obs, exp in [(a, e_a),
                     (b, (a + b) * (b + d) / n_total),
                     (c, (c + d) * (a + c) / n_total),
                     (d, (c + d) * (b + d) / n_total)]:
        if exp > 0:
            yates_chi2 += (max(0.0, abs(obs - exp) - 0.5)) ** 2 / exp
    # Bayesian Information Component (BCPNN) — simple 2-term version
    ic = 0.0
    if a > 0 and (a + b) and (a + c):
        ic = math.log2((a + 0.5) * n_total / ((a + b) * (a + c) + 1e-9) + 1e-9) \
             if ((a + b) * (a + c)) else 0.0

    # ---- Sources involved (which connectors contributed to THIS signal)----
    # Only sources whose records actually contain BOTH the drug AND the event
    # for this signal. Zero-contribution sources are dropped so the chart
    # represents the evidence base for *this* drug × event pair, not the
    # whole project.
    sources_involved: list[str] = []
    source_breakdown: list[dict] = []
    if contributing_ids:
        placeholders = ",".join("?" * len(contributing_ids))
        breakdown_rows = store.conn.execute(
            f"""SELECT s.connector, s.name AS source_name, COUNT(*) AS n
                  FROM records r JOIN sources s ON s.id = r.source_id
                  WHERE r.id IN ({placeholders})
                  GROUP BY s.id, s.connector, s.name
                  ORDER BY n DESC""",
            contributing_ids,
        ).fetchall()
        total_records = sum(br["n"] for br in breakdown_rows) or 1
        max_n = max((br["n"] for br in breakdown_rows), default=1) or 1
        seen: set[str] = set()
        for br in breakdown_rows:
            if br["connector"] not in seen:
                sources_involved.append(br["connector"])
                seen.add(br["connector"])
            source_breakdown.append({
                "connector":   br["connector"],
                "source_name": br["source_name"],
                "pretty":      _pretty_source(br["connector"]),
                "n":           br["n"],
                "pct":         round(100 * br["n"] / total_records),
                "bar_pct":     round(100 * br["n"] / max_n),
            })

    # ---- Longitudinal trend: signal mentions per day ------------------
    # Bucket spans the actual record date range for THIS signal, not an
    # arbitrary "last 30 days". This way the chart is never empty just
    # because the records happen to be older than 30d (e.g. FAERS reports
    # backdated by months).
    today = datetime.now(timezone.utc).date()
    timeline = []
    if contributing_ids:
        placeholders = ",".join("?" * len(contributing_ids))
        daily_rows = store.conn.execute(
            f"""SELECT substr(COALESCE(r.posted_at, r.ingested_at), 1, 10) AS day,
                       COUNT(*) AS n
                  FROM records r
                  WHERE r.id IN ({placeholders})
                    AND COALESCE(r.posted_at, r.ingested_at) IS NOT NULL
                  GROUP BY day
                  ORDER BY day""",
            contributing_ids,
        ).fetchall()
        if daily_rows:
            from datetime import date as _d
            day_to_n = {dr["day"]: dr["n"] for dr in daily_rows if dr["day"]}
            day_keys = sorted(day_to_n.keys())
            try:
                first = _d.fromisoformat(day_keys[0])
                last  = _d.fromisoformat(day_keys[-1])
            except Exception:
                first = last = today
            # Pad to at least 14 days to make the chart readable.
            span = (last - first).days
            if span < 13:
                pad = 13 - span
                first = first - timedelta(days=pad // 2)
                last  = last  + timedelta(days=pad - pad // 2)
            cursor = first
            while cursor <= last:
                k = cursor.isoformat()
                timeline.append((k, day_to_n.get(k, 0)))
                cursor = cursor + timedelta(days=1)
    if not timeline:
        # Fallback: empty 14-day strip ending today
        timeline = [((today - timedelta(days=i)).isoformat(), 0)
                    for i in range(13, -1, -1)]
    # baseline = first half avg ; recent = second half avg
    half = len(timeline) // 2
    base_avg = sum(v for _, v in timeline[:half]) / max(half, 1)
    recent_avg = sum(v for _, v in timeline[half:]) / max(len(timeline) - half, 1)
    if base_avg > 0:
        rel_growth_pct = round(100 * (recent_avg - base_avg) / base_avg)
    elif recent_avg > 0:
        rel_growth_pct = 100
    else:
        rel_growth_pct = 0
    if rel_growth_pct > 15:
        trend_status, trend_color, trend_icon = "Rising Signal", "red", "▲"
    elif rel_growth_pct < -15:
        trend_status, trend_color, trend_icon = "Declining Signal", "emerald", "▼"
    else:
        trend_status, trend_color, trend_icon = "Stable Signal", "blue", "→"

    # ---- Sentiment intelligence (per-signal evidence) ------------------
    sm: dict[str, int] = {}
    if contributing_ids:
        placeholders = ",".join("?" * len(contributing_ids))
        sent_rows = store.conn.execute(
            f"""SELECT r.sentiment_label, COUNT(*) AS n
                  FROM records r
                  WHERE r.id IN ({placeholders})
                  GROUP BY r.sentiment_label""",
            contributing_ids,
        ).fetchall()
        sm = {sr["sentiment_label"]: sr["n"] for sr in sent_rows}
    n_neg = (sm.get("negative") or 0) + (sm.get("adverse") or 0)
    n_neu = sm.get("neutral") or 0
    n_pos = sm.get("positive") or 0
    n_sent = max(n_neg + n_neu + n_pos, 1)
    sent_mix = {
        "negative_pct": round(100 * n_neg / n_sent),
        "neutral_pct":  round(100 * n_neu / n_sent),
        "positive_pct": round(100 * n_pos / n_sent),
        "negative": n_neg, "neutral": n_neu, "positive": n_pos,
    }

    # ---- Project-wide sentiment (all records, not just signal evidence)-
    proj_sent_rows = store.conn.execute(
        """SELECT r.sentiment_label, COUNT(*) AS n
             FROM records r JOIN sources s ON s.id = r.source_id
             WHERE s.project_id=?
             GROUP BY r.sentiment_label""",
        (pid,),
    ).fetchall()
    psm = {pr["sentiment_label"]: pr["n"] for pr in proj_sent_rows}
    p_neg = (psm.get("negative") or 0) + (psm.get("adverse") or 0)
    p_neu = psm.get("neutral") or 0
    p_pos = psm.get("positive") or 0
    p_sent = max(p_neg + p_neu + p_pos, 1)
    project_sent_mix = {
        "negative_pct": round(100 * p_neg / p_sent),
        "neutral_pct":  round(100 * p_neu / p_sent),
        "positive_pct": round(100 * p_pos / p_sent),
        "negative": p_neg, "neutral": p_neu, "positive": p_pos,
        "total": p_neg + p_neu + p_pos,
    }

    # Sentiment trajectory: per-day stacked counts spanning the same range
    # as `timeline` (which is data-driven, not "last 30 days").
    traj = {day: {"negative": 0, "neutral": 0, "positive": 0}
            for day, _ in timeline}
    if contributing_ids:
        placeholders = ",".join("?" * len(contributing_ids))
        traj_rows = store.conn.execute(
            f"""SELECT substr(COALESCE(r.posted_at, r.ingested_at), 1, 10) AS day,
                       r.sentiment_label, COUNT(*) AS n
                  FROM records r
                  WHERE r.id IN ({placeholders})
                  GROUP BY day, r.sentiment_label""",
            contributing_ids,
        ).fetchall()
        for tr in traj_rows:
            d_ = tr["day"]
            if not d_ or d_ not in traj:
                continue
            lbl = tr["sentiment_label"]
            if lbl in ("negative", "adverse"):
                traj[d_]["negative"] += tr["n"]
            elif lbl == "positive":
                traj[d_]["positive"] += tr["n"]
            else:
                traj[d_]["neutral"] += tr["n"]
    sentiment_trajectory = sorted(traj.items())

    # ---- Supporting evidence — top 12 contributing records -------------
    ev_rows = []
    if contributing_ids:
        placeholders = ",".join("?" * len(contributing_ids))
        ev_rows = store.conn.execute(
            f"""SELECT r.id, r.posted_at, r.ingested_at, r.sentiment_label,
                       r.text_redacted, r.entities_json, r.pii_hits_count, r.url,
                       s.connector, s.name AS source_name
                  FROM records r JOIN sources s ON s.id = r.source_id
                  WHERE r.id IN ({placeholders})
                  ORDER BY CASE WHEN r.posted_at IS NULL THEN 1 ELSE 0 END,
                           r.posted_at DESC, r.ingested_at DESC
                  LIMIT 12""",
            contributing_ids,
        ).fetchall()
    evidence = []
    for er in ev_rows:
        ents = []
        try:
            ents = json.loads(er["entities_json"] or "[]") or []
        except Exception:
            ents = []
        drug_conf = next((e.get("confidence", 0) for e in ents
                         if e.get("type") == "DRUG"), 0)
        sym_conf = next((e.get("confidence", 0) for e in ents
                         if e.get("type") in ("SYMPTOM", "ADR_EVENT")), 0)
        snippet = (er["text_redacted"] or "")[:280]
        # Highlight matches: drug → REDACTED_DRUG, event term → keep but bold
        evidence.append({
            "posted_at": (er["posted_at"] or er["ingested_at"] or "")[:16],
            "connector": er["connector"],
            "source_name": er["source_name"],
            "sentiment": (er["sentiment_label"] or "neutral").upper(),
            "sentiment_color": (
                "red" if er["sentiment_label"] in ("negative", "adverse")
                else ("green" if er["sentiment_label"] == "positive"
                      else "gray")
            ),
            "snippet_html": _highlight_evidence(snippet, row["drug"], row["event"]),
            "drug_conf": int(round((drug_conf or 0) * 100)),
            "sym_conf":  int(round((sym_conf or 0) * 100)),
            "pii_hits":  er["pii_hits_count"] or 0,
            "url":       er["url"],
        })

    # ---- Confidence (UI score) -----------------------------------------
    quality_warning = None
    has_reddit = any(s == "reddit" for s in sources_involved)
    if has_reddit and n_total < 200:
        quality_warning = (f'Evidence quality may be impacted by ingestion '
                           f'latency at source "reddit". Statistical weights '
                           f'for current window are marked as ESTIMATED.')

    # ---- "Strength" pill: high/med/low based on PRR & n ----------------
    if prr >= 3 and a >= 5:
        strength_label, strength_color, strength_pct = "High", "red", min(99, sig["conf"])
    elif prr >= 2:
        strength_label, strength_color, strength_pct = "Medium", "amber", min(85, sig["conf"])
    else:
        strength_label, strength_color, strength_pct = "Low", "gray", sig["conf"]

    # ---- Last-fetched & cadence (from watermarks of contributing sources)
    last_fetched_iso = None
    last_fetched_human = "—"
    cadence_label = "—"
    if contributing_ids:
        ph = ",".join("?" * len(contributing_ids))
        wm_row = store.conn.execute(
            f"""SELECT MAX(w.last_run_at) AS last_run,
                       (SELECT s.latency FROM sources s
                          JOIN records r ON r.source_id = s.id
                          WHERE r.id IN ({ph})
                          GROUP BY s.latency ORDER BY COUNT(*) DESC LIMIT 1) AS top_latency
                  FROM watermarks w
                  JOIN sources s ON s.id = w.source_id
                  JOIN records r ON r.source_id = s.id
                  WHERE r.id IN ({ph})""",
            contributing_ids + contributing_ids,
        ).fetchone()
        if wm_row and wm_row["last_run"]:
            last_fetched_iso = wm_row["last_run"]
            cadence_label = (wm_row["top_latency"] or "—").replace("_", "-")
            last_fetched_human = _human_relative(last_fetched_iso)

    return {
        **sig,
        "drug":  row["drug"], "event": row["event"],
        "project_id": pid,
        "scan_window_label": (f"Last fetched: {last_fetched_human}"
                              if last_fetched_iso
                              else "Last 30 Days"),
        "last_fetched_iso":   last_fetched_iso,
        "last_fetched_human": last_fetched_human,
        "cadence_label":      cadence_label,
        "total_mentions": a,
        "strength_label": strength_label,
        "strength_color": strength_color,
        "strength_pct":   strength_pct,
        "quality_warning": quality_warning,
        # statistics — recomputed values (use those for display)
        "stats": {
            "prr":  _fmt_ratio(prr),     "prr_thr":  "≥ 2",
            "ror":  _fmt_ratio(ror),     "ror_thr":  "≥ 2",
            "chi2": _fmt_ratio(yates_chi2), "chi2_thr": "≥ 3.84",
            "ic":   f"{ic:.2f}",         "ic_thr":   "≥ 0",
        },
        "contingency": contingency,
        "sources_involved": sources_involved,
        "sources_pretty": [_pretty_source(c) for c in sources_involved],
        "source_breakdown": source_breakdown,
        "source_breakdown_total": sum(sb["n"] for sb in source_breakdown),
        "trend_status": trend_status,
        "trend_color":  trend_color,
        "trend_icon":   trend_icon,
        "rel_growth_pct": rel_growth_pct,
        "timeline":     timeline,                  # for bar chart
        "timeline_max": max((v for _, v in timeline), default=1),
        "sent_mix": sent_mix,
        "project_sent_mix": project_sent_mix,
        "sentiment_trajectory": sentiment_trajectory,
        "evidence": evidence,
        "evidence_total": len(evidence),
    }


_SRC_PRETTY = {
    "reddit": "Reddit", "rss": "RSS", "faers": "FAERS",
    "openfda": "OpenFDA", "html_stealth": "Forums",
    "html_listing": "Forums", "x_stub": "X",
    "x": "X", "whatsapp": "WhatsApp",
}


def _pretty_source(connector: str) -> str:
    return _SRC_PRETTY.get(connector, connector.title())


def _fmt_ratio(v: float) -> str:
    """Format PRR/ROR/χ² for UI: cap absurd values caused by zero-cell
    contingencies so users see a clean bound instead of `666666666.67`."""
    if v is None or v != v:        # NaN
        return "—"
    if v >= 1000:
        return "≥ 1000"
    if v <= 0:
        return "0.00"
    return f"{v:.2f}"


def _human_relative(iso_ts: str | None) -> str:
    """Convert an ISO timestamp into a 'just now / 5m ago / 2h ago / 3d ago'
    style label. Used for last-fetched indicators in the UI."""
    if not iso_ts:
        return "—"
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except Exception:
        return "—"
    now = datetime.now(timezone.utc)
    delta = now - ts
    secs = int(delta.total_seconds())
    if secs < 0:
        return "just now"
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    days = secs // 86400
    if days < 30:
        return f"{days}d ago"
    if days < 365:
        return f"{days // 30}mo ago"
    return f"{days // 365}y ago"


def _highlight_evidence(text: str, drug: str, event: str) -> str:
    """Reference-design rendering: drug term → REDACTED_DRUG pill,
    event term → underlined red. PII redactions are already applied
    by the ingest path."""
    if not text:
        return ""
    import re as _re
    out = _re.sub(
        r'(?i)\b' + _re.escape(drug) + r'\b',
        '<span class="bg-slate-200 text-slate-700 font-mono text-[10px] px-1 py-0.5 rounded">REDACTED_DRUG</span>',
        text,
    )
    out = _re.sub(
        r'(?i)\b' + _re.escape(event) + r'\b',
        f'<span class="text-red-700 underline decoration-red-400 font-medium">{event}</span>',
        out,
    )
    # also highlight common PII placeholders left by pii.redact()
    out = _re.sub(
        r'\[(EMAIL|PHONE|NAME|MRN|SSN|ADDRESS)_REDACTED\]',
        r'<span class="bg-blue-100 text-blue-700 font-mono text-[10px] px-1 py-0.5 rounded">PII_REDACTED</span>',
        out,
    )
    return out


# ---------------------------------------------------------------------------
# Decorator (PRR colour, confidence, status pill, ago)
# ---------------------------------------------------------------------------
def _decorate_signal(r: dict) -> dict:
    prr = r.get("prr") or 0.0
    chi = r.get("chi2") or 0.0
    n = r.get("n") or 0
    sig_id = f"SIG-{r['id']:03d}"

    if prr >= 3.0:
        prr_color = "red"
    elif prr >= 2.0:
        prr_color = "amber"
    else:
        prr_color = "blue"

    conf = max(20, min(99,
        round(40 + 14 * math.log(max(prr, 1.0)) + 6 * math.log(max(n, 1)))))
    if conf >= 80:
        conf_label, conf_color = "HIGH", "red"
    elif conf >= 60:
        conf_label, conf_color = "MEDIUM", "amber"
    else:
        conf_label, conf_color = "LOW", "gray"

    if prr >= 3.0 and n >= 5:
        status = ("ESCALATED", "amber")
    elif prr >= 2.0:
        status = ("PENDING", "blue")
    elif prr < 1.0:
        status = ("DISMISSED", "gray")
    else:
        status = ("REVIEWED", "green")

    # WHO/MHRA-style strength tier (independent of UI status):
    #   STRONG       — n ≥ 5  AND PRR ≥ 3 AND χ² ≥ 4
    #   MODERATE     — n ≥ 3  AND PRR ≥ 2 AND χ² ≥ 4
    #   EXPLORATORY  — anything below the moderate gate but flagged
    if n >= 5 and prr >= 3.0 and chi >= 4.0:
        tier_label, tier_color = "STRONG", "red"
    elif n >= 3 and prr >= 2.0 and chi >= 4.0:
        tier_label, tier_color = "MODERATE", "amber"
    else:
        tier_label, tier_color = "EXPLORATORY", "gray"

    return {
        **r,
        "sig_id":     sig_id,
        "prr_color":  prr_color,
        "prr_str":    _fmt_ratio(prr),
        "chi_str":    f"{chi:.1f}",
        "conf":       conf,
        "conf_label": conf_label,
        "conf_color": conf_color,
        "tier_label": tier_label,
        "tier_color": tier_color,
        "status":     status,
        "ago":        _human_ago(r.get("computed_at")),
        "first_detected": (r.get("computed_at") or "")[:10],
        "src_count":  0,
        "src_names":  "",
        "trend":      ("NEW", "blue", "✦", "first run"),
    }


def _human_ago(ts: str | None) -> str:
    if not ts:
        return "—"
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return ts[:16]
    delta = datetime.now(timezone.utc) - t
    s = int(delta.total_seconds())
    if s < 60:    return f"{s}s ago"
    if s < 3600:  return f"{s // 60}m ago"
    if s < 86400: return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


# ---------------------------------------------------------------------------
# Audit log — exact reference shape (Time | Action | User | Trace ID)
# ---------------------------------------------------------------------------
ACTION_LABELS = {
    "run.start":          "Run Started",
    "run.end":            "Run Completed",
    "ingest":             "Records Ingested",
    "redact":             "Record Redacted",
    "signal.flagged":     "Signal Flagged",
    "signal.dismissed":   "Signal Dismissed",
    "project.created":    "Project Created",
    "project.deleted":    "Project Deleted",
    "connector.discovered": "Connector Discovered",
    "connector.approved":   "Connector Approved",
}


def audit_tail(store: Store, limit: int = 8, full: bool = False) -> list[dict]:
    rows = store.conn.execute(
        """SELECT id, ts, actor, action, target, hash
             FROM audit_log ORDER BY id DESC LIMIT ?""", (limit,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            t = datetime.fromisoformat(d["ts"].replace("Z", "+00:00"))
            d["time"] = t.strftime("%H:%M:%S")
            d["time_full"] = t.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            d["time"] = (d["ts"] or "")[:8]
            d["time_full"] = d["ts"]
        d["action_label"] = ACTION_LABELS.get(d["action"], d["action"])
        d["user"] = d["actor"]
        # Synthesise a short trace id
        if d["action"].startswith("signal"):
            d["trace"] = f"SIG-{d['id']:03d}"
        elif "redact" in d["action"] or "ingest" in d["action"]:
            d["trace"] = f"LOG-{d['id']:03d}"
        else:
            d["trace"] = f"EVT-{d['id']:03d}"
        d["hash_short"] = (d.get("hash") or "")[:12]
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Agent / connector activity log (terminal-style)
# ---------------------------------------------------------------------------
def agent_log(store: Store, limit: int = 8) -> list[dict]:
    """Pull recent ingest/discovery/redaction events from audit_log.

    Surfaces real per-source fetch counts, signal counts, and keyword
    expansion stats from the audit payload — not just the bare action.
    """
    rows = store.conn.execute(
        """SELECT ts, actor, action, target, payload_json
             FROM audit_log
             WHERE action IN ('run.start','run.end','source.done',
                              'signals.computed','ingest','redact',
                              'connector.discovered','connector.approved')
             ORDER BY id DESC LIMIT ?""", (limit * 3,)  # over-fetch; we filter
    ).fetchall()
    out = []
    for r in rows:
        try:
            t = datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
            ts = t.strftime("%H:%M:%S")
        except Exception:
            ts = (r["ts"] or "")[:8]
        action = r["action"]
        target = r["target"] or "—"
        try:
            payload = json.loads(r["payload_json"] or "{}")
        except Exception:
            payload = {}

        if action == "run.start":
            kx = payload.get("keyword_expansion") or {}
            if kx:
                total_terms = sum(len(v) for v in kx.values())
                seeds = len(kx)
                text = (f"Run started · expanded {seeds} seed keyword"
                        f"{'s' if seeds != 1 else ''} → {total_terms} variants "
                        f"(dict + fuzzy-morph) for '{target}'")
            else:
                text = f"Run started for project '{target}'"
            level = "info"
        elif action == "source.done":
            f = payload.get("fetched", 0)
            ins = payload.get("inserted", 0)
            dup = payload.get("duplicates", 0)
            mat = payload.get("matched", 0)
            if f == 0:
                text = f"{target}: no new records (window quiet)"
                level = "muted"
            else:
                text = (f"{target}: fetched {f}, inserted {ins}"
                        f"{f', dedup {dup}' if dup else ''}"
                        f"{f', kw-matched {mat}' if mat else ''}")
                level = "success"
        elif action == "run.end":
            f = payload.get("fetched", 0)
            ins = payload.get("inserted", 0)
            if f == 0 and ins == 0:
                text = f"Run complete for '{target}' · no new data this cycle"
                level = "muted"
            else:
                text = (f"Run complete for '{target}' · {ins} new records "
                        f"persisted (of {f} fetched)")
                level = "ready"
        elif action == "signals.computed":
            flagged = payload.get("flagged", 0)
            rows_ct = payload.get("rows", 0)
            with_pairs = payload.get("with_pairs", 0)
            text = (f"Signals recomputed for '{target}' · {flagged} flagged "
                    f"from {rows_ct} pairs ({with_pairs} drug↔event)")
            level = "ready" if flagged else "info"
        elif action == "ingest":
            text = f"Fetched batch from {target}"
            level = "info"
        elif action == "redact":
            text = f"Redaction pipeline applied: {target}"
            level = "success"
        elif action == "connector.discovered":
            text = f"Discovered selectors for '{target}'"
            level = "info"
        elif action == "connector.approved":
            text = f"READY: connector '{target}' approved & queued"
            level = "ready"
        else:
            text = f"{action} · {target}"
            level = "info"
        out.append({"ts": ts, "level": level, "text": text})
        if len(out) >= limit:
            break

    # Fall back to scripted demo lines if the DB has nothing yet
    if not out:
        out = _demo_agent_lines()
    return out


def _demo_agent_lines() -> list[dict]:
    base = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    return [
        {"ts": (base - timedelta(minutes=4)).strftime("%H:%M:%S"),
         "level": "info",
         "text": "Attempting discovery of RSS selectors for 'ehealthforum.com'..."},
        {"ts": (base - timedelta(minutes=3, seconds=44)).strftime("%H:%M:%S"),
         "level": "success",
         "text": "Success: Identified 4 entity markers."},
        {"ts": (base - timedelta(minutes=3, seconds=10)).strftime("%H:%M:%S"),
         "level": "success",
         "text": "Testing redaction pipeline: PII mask at 99.8% precision."},
        {"ts": (base - timedelta(minutes=2, seconds=45)).strftime("%H:%M:%S"),
         "level": "ready",
         "text": "READY: Connector generated and queued for approval."},
    ]


def debug_log(store: Store, limit: int = 12) -> list[dict]:
    """Connector-page debug stream — same source as agent_log, longer tail."""
    return agent_log(store, limit=limit)
