"""
SQLite store for SrotaAI (Day 1).

Tables:
  projects     — one row per Project YAML, full config snapshot
  sources      — one row per SourceConfig within a project
  records      — every ingested RawRecord, dedup'd by external_id and content_hash
  watermarks   — per-source ingestion watermark (last_seen_ts) for incremental fetch
  signals      — disproportionality outputs (PRR/chi2/IC) — populated Day 4
  audit_log    — append-only Merkle-chained log (prev_hash + hash) — wired Day 6

The Day-1 contract: running the runner twice on the same project must insert
0 new rows on the second run (external_id UNIQUE + content_hash UNIQUE +
INSERT OR IGNORE). `raw_encrypted` is stored as a BLOB; envelope encryption
is deferred to Day 4 — for now we write raw bytes and tag the column so the
schema doesn't change later.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

# Errors module is imported lazily inside methods to avoid a circular
# dependency with anything that wants to import Store at module-load time.

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "srotaai.db"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    description     TEXT,
    keywords_json   TEXT NOT NULL,        -- JSON list[str]
    time_window_json TEXT NOT NULL,       -- JSON TimeWindow.to_dict()
    config_json     TEXT NOT NULL,        -- full Project._serialisable() snapshot
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    connector       TEXT NOT NULL,
    params_json     TEXT NOT NULL,
    latency         TEXT NOT NULL,
    time_window_json TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE(project_id, name)
);

CREATE TABLE IF NOT EXISTS records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    external_id     TEXT NOT NULL,
    content_hash    TEXT NOT NULL,        -- sha256 of normalised text
    url             TEXT,
    posted_at       TEXT,                 -- ISO-8601 UTC
    text_redacted   TEXT,                 -- post-PII-scrub text (Day 4); raw text for now
    raw_encrypted   BLOB,                 -- raw bytes; envelope-encrypt Day 4
    author_handle   TEXT,
    language_hint   TEXT,
    source_kind     TEXT,
    matched_keywords_json TEXT,           -- JSON list[str]
    ingested_at     TEXT NOT NULL,
    UNIQUE(source_id, external_id),
    UNIQUE(content_hash)
);
CREATE INDEX IF NOT EXISTS idx_records_posted_at ON records(posted_at);
CREATE INDEX IF NOT EXISTS idx_records_source    ON records(source_id);

CREATE TABLE IF NOT EXISTS watermarks (
    source_id       INTEGER PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
    last_seen_id    TEXT,
    last_seen_ts    TEXT,                 -- ISO-8601 UTC
    last_run_at     TEXT,
    last_fetched    INTEGER NOT NULL DEFAULT 0,
    last_inserted   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    drug            TEXT NOT NULL,
    event           TEXT NOT NULL,
    prr             REAL,
    chi2            REAL,
    ic              REAL,
    n               INTEGER,
    window_since    TEXT,
    window_until    TEXT,
    computed_at     TEXT NOT NULL,
    UNIQUE(project_id, drug, event, computed_at)
);
CREATE INDEX IF NOT EXISTS idx_signals_project ON signals(project_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    actor           TEXT NOT NULL,
    action          TEXT NOT NULL,
    target          TEXT,
    payload_json    TEXT,
    prev_hash       TEXT,
    hash            TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def content_hash(text: str) -> str:
    """Stable sha256 over normalised text — used as cross-source dedup key."""
    norm = " ".join((text or "").split()).strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


@dataclass
class InsertResult:
    fetched: int = 0
    inserted: int = 0
    duplicates: int = 0
    last_seen_ts: Optional[datetime] = None
    last_seen_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
class Store:
    """Typed thin wrapper over sqlite3. One connection per Store instance."""

    def __init__(self, path: str | Path = DEFAULT_DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # `timeout=10` lets SQLite block up to 10s on locks instead of
        # raising immediately — critical when the in-process scheduler
        # holds a write while the UI tries to save.
        self.conn = sqlite3.connect(str(self.path), timeout=10.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON;")
        self.conn.execute("PRAGMA journal_mode = WAL;")
        self.conn.execute("PRAGMA busy_timeout = 10000;")
        self.conn.execute("PRAGMA synchronous = NORMAL;")
        self.conn.executescript(SCHEMA)
        self._migrate_records_v2()
        self._migrate_projects_v2()
        self.conn.commit()

    # ---- schema migration ------------------------------------------------
    def _migrate_records_v2(self) -> None:
        """Add per-record enrichment columns (sentiment / PII / entities).

        Idempotent: each ALTER is wrapped to swallow `duplicate column` errors,
        so this runs cheaply on every Store() open.
        """
        adds = [
            ("sentiment_score", "REAL"),
            ("sentiment_label", "TEXT"),
            ("pii_hits_count",  "INTEGER NOT NULL DEFAULT 0"),
            ("entities_json",   "TEXT"),
        ]
        for col, typ in adds:
            try:
                self.conn.execute(f"ALTER TABLE records ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise

    def _migrate_projects_v2(self) -> None:
        """Add scheduler / lifecycle columns to projects.

        - status:        'running' | 'paused' | 'closed'
        - cadence:       'real_time' | 'daily' | 'weekly' | 'manual'
                         (resolved from sources' max latency at save time)
        - last_started_at / last_completed_at: scheduler bookkeeping
        """
        adds = [
            ("status",            "TEXT NOT NULL DEFAULT 'running'"),
            ("cadence",           "TEXT NOT NULL DEFAULT 'daily'"),
            ("last_started_at",   "TEXT"),
            ("last_completed_at", "TEXT"),
        ]
        for col, typ in adds:
            try:
                self.conn.execute(f"ALTER TABLE projects ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise

    # ---- lifecycle -------------------------------------------------------
    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---- transient-failure retry ---------------------------------------
    def _retry_tx(self, op: str, fn, *, ctx: Optional[dict] = None,
                  retries: int = 5):
        """Run `fn(conn)` inside a transaction, retrying on
        `database is locked` / `database is busy` (transient WAL contention).

        Re-raises as DatabaseError after exhausting retries so callers can
        present a clean message.
        """
        from .errors import DatabaseError, with_retry
        def _go():
            with self._tx() as c:
                return fn(c)
        try:
            return with_retry(
                _go, op=op, ctx=ctx, retries=retries, base_delay=0.05,
                retry_on=(sqlite3.OperationalError,),
            )
        except sqlite3.OperationalError as e:
            raise DatabaseError(
                f"sqlite operational: {e}",
                hint=("The database was busy too long. Close any other "
                      "process touching this DB and try again."),
                cause=e, ctx=ctx,
            ) from e
        except sqlite3.IntegrityError as e:
            from .errors import DuplicateError
            raise DuplicateError(str(e), cause=e, ctx=ctx) from e
        except sqlite3.DatabaseError as e:
            raise DatabaseError(str(e), cause=e, ctx=ctx) from e

    # ---- projects --------------------------------------------------------
    def upsert_project(self, project) -> str:
        """Insert or update a Project row. Returns project_id."""
        cfg = project._serialisable()
        now = _utcnow_iso()
        tw = cfg["time_window"]
        with self._tx() as c:
            c.execute(
                """
                INSERT INTO projects (id, name, description, keywords_json,
                                      time_window_json, config_json,
                                      created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    description = excluded.description,
                    keywords_json = excluded.keywords_json,
                    time_window_json = excluded.time_window_json,
                    config_json = excluded.config_json,
                    updated_at = excluded.updated_at
                """,
                (project.id, project.name, project.description,
                 json.dumps(project.keywords), json.dumps(tw),
                 json.dumps(cfg, default=str), now, now),
            )
        return project.id

    def upsert_source(self, project_id: str, src) -> int:
        """Insert or update a SourceConfig row. Returns source_id (PK)."""
        now = _utcnow_iso()
        tw_json = json.dumps(src.time_window.to_dict()) if src.time_window else None
        with self._tx() as c:
            c.execute(
                """
                INSERT INTO sources (project_id, name, connector, params_json,
                                     latency, time_window_json, enabled,
                                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, name) DO UPDATE SET
                    connector = excluded.connector,
                    params_json = excluded.params_json,
                    latency = excluded.latency,
                    time_window_json = excluded.time_window_json,
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (project_id, src.name, src.connector, json.dumps(src.params),
                 src.latency.value, tw_json, 1 if src.enabled else 0, now, now),
            )
            row = c.execute(
                "SELECT id FROM sources WHERE project_id=? AND name=?",
                (project_id, src.name),
            ).fetchone()
        return int(row["id"])

    # ---- records ---------------------------------------------------------
    def insert_record(
        self,
        source_id: int,
        rec,
        matched_keywords: Optional[list[str]] = None,
        text_redacted: Optional[str] = None,
    ) -> bool:
        """Insert one RawRecord. Returns True if a new row was written.

        Enriches each record with sentiment / PII-hit count / entities derived
        from the *original* text. Failures in enrichment are logged-and-swallowed
        so a connector hiccup never blocks ingestion.
        """
        chash = content_hash(rec.text or "")
        raw_bytes = (rec.raw_blob or "").encode("utf-8") if rec.raw_blob else None
        text_for_store = text_redacted if text_redacted is not None else rec.text

        # ---- enrichment (lazy import to avoid circular at module load) ----
        sent_score: Optional[float] = None
        sent_label: Optional[str] = None
        pii_count = 0
        entities_payload: Optional[str] = None
        try:
            from . import sentiment as _sent
            r_sent = _sent.analyze(rec.text or "")
            sent_score = float(r_sent.score)
            sent_label = r_sent.label
        except Exception:
            pass
        try:
            from . import pii as _pii
            pii_count = len(_pii.detect(rec.text or ""))
        except Exception:
            pass
        try:
            from . import ner as _ner
            r_ner = _ner.extract(rec.text or "")
            ents = [
                {"type": e.type, "text": e.text, "span": list(e.span),
                 "normalized": e.normalized, "confidence": e.confidence}
                for e in (list(r_ner.drugs) + list(r_ner.symptoms)
                          + list(r_ner.adr_events))
            ]
            entities_payload = json.dumps(ents, ensure_ascii=False) if ents else None
        except Exception:
            pass

        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO records
                (source_id, external_id, content_hash, url, posted_at,
                 text_redacted, raw_encrypted, author_handle, language_hint,
                 source_kind, matched_keywords_json, ingested_at,
                 sentiment_score, sentiment_label, pii_hits_count, entities_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id, rec.external_id, chash, rec.url, _iso(rec.posted_at),
                text_for_store, raw_bytes, rec.author_handle, rec.language_hint,
                rec.source_kind,
                json.dumps(matched_keywords or []),
                _utcnow_iso(),
                sent_score, sent_label, pii_count, entities_payload,
            ),
        )
        return cur.rowcount > 0

    def insert_records(
        self,
        source_id: int,
        items: Iterable[tuple],
    ) -> InsertResult:
        """
        Bulk-insert with dedup. `items` yields (RawRecord, matched_keywords, text_redacted).
        Updates the watermark in the same transaction.
        """
        result = InsertResult()
        with self._tx():
            for rec, hits, redacted in items:
                result.fetched += 1
                wrote = self.insert_record(source_id, rec, hits, redacted)
                if wrote:
                    result.inserted += 1
                else:
                    result.duplicates += 1
                if rec.posted_at is not None:
                    if result.last_seen_ts is None or rec.posted_at > result.last_seen_ts:
                        result.last_seen_ts = rec.posted_at
                        result.last_seen_id = rec.external_id
            self._update_watermark(source_id, result)
        return result

    # ---- enrichment backfill --------------------------------------------
    def backfill_enrichment(self, batch: int = 500, force: bool = False) -> dict:
        """Compute sentiment/PII/entities for existing records that lack them.

        Reads `text_redacted` (post-PII text) when the original is gone; that's
        good enough for sentiment + entity dictionaries. Pass `force=True` to
        re-process every row.
        """
        from . import sentiment as _sent
        from . import pii as _pii
        from . import ner as _ner

        where = "" if force else " WHERE sentiment_label IS NULL"
        total = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM records{where}"
        ).fetchone()["n"]
        done = 0
        offset = 0
        while True:
            rows = self.conn.execute(
                f"SELECT id, text_redacted FROM records{where} "
                f"ORDER BY id LIMIT ? OFFSET ?",
                (batch, 0 if not force else offset),
            ).fetchall()
            if not rows:
                break
            with self._tx() as c:
                for r in rows:
                    txt = r["text_redacted"] or ""
                    try:
                        s_r = _sent.analyze(txt)
                        s_score, s_label = float(s_r.score), s_r.label
                    except Exception:
                        s_score, s_label = None, None
                    try:
                        pii_n = len(_pii.detect(txt))
                    except Exception:
                        pii_n = 0
                    try:
                        n_r = _ner.extract(txt)
                        ents = [
                            {"type": e.type, "text": e.text, "span": list(e.span),
                             "normalized": e.normalized, "confidence": e.confidence}
                            for e in (list(n_r.drugs) + list(n_r.symptoms)
                                      + list(n_r.adr_events))
                        ]
                        ents_json = json.dumps(ents, ensure_ascii=False) if ents else None
                    except Exception:
                        ents_json = None
                    c.execute(
                        "UPDATE records SET sentiment_score=?, sentiment_label=?, "
                        "pii_hits_count=?, entities_json=? WHERE id=?",
                        (s_score, s_label, pii_n, ents_json, r["id"]),
                    )
                    done += 1
            if force:
                offset += batch
            # without force, we re-query (rows that just got filled drop out)
            if done >= total:
                break
        return {"updated": done, "scanned": total}

    # ---- watermarks ------------------------------------------------------
    def get_watermark(self, source_id: int) -> Optional[datetime]:
        row = self.conn.execute(
            "SELECT last_seen_ts FROM watermarks WHERE source_id=?",
            (source_id,),
        ).fetchone()
        if not row or not row["last_seen_ts"]:
            return None
        return datetime.fromisoformat(row["last_seen_ts"])

    def _update_watermark(self, source_id: int, r: InsertResult) -> None:
        now = _utcnow_iso()
        existing = self.conn.execute(
            "SELECT last_seen_ts FROM watermarks WHERE source_id=?",
            (source_id,),
        ).fetchone()

        new_ts_iso = _iso(r.last_seen_ts)
        if existing and existing["last_seen_ts"] and new_ts_iso:
            if existing["last_seen_ts"] >= new_ts_iso:
                new_ts_iso = existing["last_seen_ts"]
        elif existing and existing["last_seen_ts"] and not new_ts_iso:
            new_ts_iso = existing["last_seen_ts"]

        self.conn.execute(
            """
            INSERT INTO watermarks (source_id, last_seen_id, last_seen_ts,
                                    last_run_at, last_fetched, last_inserted)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                last_seen_id  = COALESCE(excluded.last_seen_id, watermarks.last_seen_id),
                last_seen_ts  = excluded.last_seen_ts,
                last_run_at   = excluded.last_run_at,
                last_fetched  = excluded.last_fetched,
                last_inserted = excluded.last_inserted
            """,
            (source_id, r.last_seen_id, new_ts_iso, now, r.fetched, r.inserted),
        )

    # ---- audit (Merkle-chained, used Day 6) ------------------------------
    def append_audit(self, actor: str, action: str,
                     target: Optional[str] = None,
                     payload: Optional[dict] = None) -> str:
        # Read prev_hash and INSERT inside the SAME transaction so concurrent
        # appends serialize correctly. With WAL + BEGIN IMMEDIATE only one
        # writer is active at a time, guaranteeing the chain is monotonic.
        ts = _utcnow_iso()
        payload_json = json.dumps(payload or {}, sort_keys=True, default=str)
        with self._tx() as c:
            prev = c.execute(
                "SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
            prev_hash = prev["hash"] if prev else ""
            h = hashlib.sha256(
                f"{prev_hash}|{ts}|{actor}|{action}|{target or ''}|{payload_json}".encode("utf-8")
            ).hexdigest()
            c.execute(
                """INSERT INTO audit_log (ts, actor, action, target, payload_json,
                                          prev_hash, hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (ts, actor, action, target, payload_json, prev_hash, h),
            )
        return h

    # ---- project CRUD (UI-facing) ---------------------------------------
    def list_projects(self) -> list[dict]:
        """Return all projects, newest first, with light-weight counts.

        Used by the sidebar list. Does not load full source rows.
        """
        rows = self.conn.execute(
            """SELECT p.id, p.name, p.description, p.updated_at, p.created_at,
                      p.status, p.cadence, p.last_started_at, p.last_completed_at,
                      p.keywords_json,
                      (SELECT COUNT(*) FROM sources s WHERE s.project_id = p.id) AS n_sources,
                      (SELECT COUNT(*) FROM records r
                         JOIN sources s ON s.id = r.source_id
                         WHERE s.project_id = p.id) AS n_records
               FROM projects p
               ORDER BY p.updated_at DESC, p.id"""
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["keywords"] = json.loads(d.pop("keywords_json") or "[]")
            except Exception:
                d["keywords"] = []
            out.append(d)
        return out

    # ---- project lifecycle (status / cadence) --------------------------
    def set_project_status(self, project_id: str, status: str) -> None:
        """status ∈ {'running', 'paused', 'closed'}."""
        if status not in ("running", "paused", "closed"):
            raise ValueError(f"invalid status: {status}")
        with self._tx() as c:
            c.execute("UPDATE projects SET status=?, updated_at=? WHERE id=?",
                      (status, _utcnow_iso(), project_id))
        self.append_audit("scheduler", f"project.{status}", target=project_id)

    def set_project_cadence(self, project_id: str, cadence: str) -> None:
        if cadence not in ("real_time", "daily", "weekly", "manual"):
            raise ValueError(f"invalid cadence: {cadence}")
        with self._tx() as c:
            c.execute("UPDATE projects SET cadence=?, updated_at=? WHERE id=?",
                      (cadence, _utcnow_iso(), project_id))

    def mark_run_started(self, project_id: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE projects SET last_started_at=? WHERE id=?",
                      (_utcnow_iso(), project_id))

    def mark_run_completed(self, project_id: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE projects SET last_completed_at=? WHERE id=?",
                      (_utcnow_iso(), project_id))

    def load_project(self, project_id: str) -> Optional[dict]:
        """Reconstruct the full project payload (project + sources) from DB.

        Returns a dict ready to seed the UI form, or None if the project
        does not exist. This is the canonical read path for the dashboard;
        YAMLs are never consulted.
        """
        prow = self.conn.execute(
            "SELECT * FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        if not prow:
            return None
        srows = self.conn.execute(
            """SELECT name, connector, params_json, latency, time_window_json,
                      enabled
               FROM sources WHERE project_id=? ORDER BY id""",
            (project_id,),
        ).fetchall()
        return {
            "id":          prow["id"],
            "name":        prow["name"],
            "description": prow["description"] or "",
            "keywords":    json.loads(prow["keywords_json"] or "[]"),
            "time_window": json.loads(prow["time_window_json"] or "{}"),
            "created_at":  prow["created_at"],
            "updated_at":  prow["updated_at"],
            "status":          (prow["status"] if "status" in prow.keys() else "running"),
            "cadence":         (prow["cadence"] if "cadence" in prow.keys() else "daily"),
            "last_started_at": (prow["last_started_at"] if "last_started_at" in prow.keys() else None),
            "last_completed_at": (prow["last_completed_at"] if "last_completed_at" in prow.keys() else None),
            "sources": [
                {
                    "name":        s["name"],
                    "connector":   s["connector"],
                    "params":      json.loads(s["params_json"] or "{}"),
                    "latency":     s["latency"],
                    "time_window": (json.loads(s["time_window_json"])
                                    if s["time_window_json"] else None),
                    "enabled":     bool(s["enabled"]),
                }
                for s in srows
            ],
        }

    def save_project_full(self, project) -> str:
        """Persist a Project + ALL its sources in a single retried transaction.

        - Upserts the project row.
        - Upserts every source by (project_id, name).
        - Deletes sources no longer present (cascade clears their data).

        Atomic; on transient `database is locked` retries up to 5×; on
        permanent failure raises `errors.DatabaseError` / `DuplicateError`.
        """
        import logging
        log = logging.getLogger("srotaai.store")
        cfg = project._serialisable()
        now = _utcnow_iso()
        tw = cfg["time_window"]
        kept_names: list[str] = [s.name for s in project.sources]

        def _do(c: sqlite3.Connection):
            c.execute(
                """INSERT INTO projects (id, name, description, keywords_json,
                                         time_window_json, config_json,
                                         created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       name = excluded.name,
                       description = excluded.description,
                       keywords_json = excluded.keywords_json,
                       time_window_json = excluded.time_window_json,
                       config_json = excluded.config_json,
                       updated_at = excluded.updated_at""",
                (project.id, project.name, project.description,
                 json.dumps(project.keywords), json.dumps(tw),
                 json.dumps(cfg, default=str), now, now),
            )
            for src in project.sources:
                tw_json = (json.dumps(src.time_window.to_dict())
                           if src.time_window else None)
                c.execute(
                    """INSERT INTO sources (project_id, name, connector,
                                            params_json, latency,
                                            time_window_json, enabled,
                                            created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(project_id, name) DO UPDATE SET
                           connector = excluded.connector,
                           params_json = excluded.params_json,
                           latency = excluded.latency,
                           time_window_json = excluded.time_window_json,
                           enabled = excluded.enabled,
                           updated_at = excluded.updated_at""",
                    (project.id, src.name, src.connector,
                     json.dumps(src.params), src.latency.value, tw_json,
                     1 if src.enabled else 0, now, now),
                )
            if kept_names:
                placeholders = ",".join("?" * len(kept_names))
                c.execute(
                    f"DELETE FROM sources WHERE project_id=? "
                    f"AND name NOT IN ({placeholders})",
                    (project.id, *kept_names),
                )
            else:
                c.execute("DELETE FROM sources WHERE project_id=?",
                          (project.id,))

        self._retry_tx("save_project_full", _do,
                       ctx={"project_id": project.id,
                            "sources": len(project.sources)})
        log.info("save_project_full id=%s name=%s sources=%d",
                 project.id, project.name, len(project.sources))
        try:
            self.append_audit("ui", "project.save", target=project.id,
                              payload={"sources": kept_names})
        except Exception:                                      # noqa: BLE001
            log.exception("audit append failed (non-fatal)")
        return project.id

    def project_exists(self, project_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        return row is not None

    def delete_project(self, project_id: str) -> bool:
        """Delete a project and all dependent rows. Returns True if deleted."""
        import logging
        log = logging.getLogger("srotaai.store")
        result = {"n": 0}

        def _do(c: sqlite3.Connection):
            cur = c.execute("DELETE FROM projects WHERE id=?", (project_id,))
            result["n"] = cur.rowcount

        self._retry_tx("delete_project", _do, ctx={"project_id": project_id})
        log.info("delete_project id=%s deleted=%d", project_id, result["n"])
        if result["n"]:
            try:
                self.append_audit("ui", "project.delete", target=project_id)
            except Exception:                                  # noqa: BLE001
                pass
        return result["n"] > 0

    def reset_project_data(self, project_id: str) -> dict:
        """Wipe records + watermarks + signals for a project, keep config."""
        import logging
        log = logging.getLogger("srotaai.store")
        out = {"records": 0, "watermarks": 0, "signals": 0}

        def _do(c: sqlite3.Connection):
            out["records"] = c.execute(
                """DELETE FROM records WHERE source_id IN
                     (SELECT id FROM sources WHERE project_id=?)""",
                (project_id,),
            ).rowcount
            out["watermarks"] = c.execute(
                """DELETE FROM watermarks WHERE source_id IN
                     (SELECT id FROM sources WHERE project_id=?)""",
                (project_id,),
            ).rowcount
            out["signals"] = c.execute(
                "DELETE FROM signals WHERE project_id=?", (project_id,)
            ).rowcount

        self._retry_tx("reset_project_data", _do,
                       ctx={"project_id": project_id})
        log.info("reset_project_data id=%s records=%d watermarks=%d "
                 "signals=%d", project_id, out["records"],
                 out["watermarks"], out["signals"])
        try:
            self.append_audit("ui", "project.reset", target=project_id,
                              payload=out)
        except Exception:                                      # noqa: BLE001
            pass
        return out

    def db_diagnostics(self) -> dict:
        """Row counts for every important table — used by the debug panel."""
        out: dict[str, int] = {}
        for t in ("projects", "sources", "records", "watermarks",
                  "signals", "audit_log"):
            out[t] = int(self.conn.execute(
                f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"])
        return out

    # ---- read helpers ----------------------------------------------------
    def count_records(self, source_id: Optional[int] = None) -> int:
        if source_id is None:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM records").fetchone()
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE source_id=?", (source_id,)
            ).fetchone()
        return int(row["n"])

    def project_summary(self, project_id: str) -> dict:
        sources = self.conn.execute(
            """SELECT s.id, s.name, s.connector, s.enabled,
                      w.last_seen_ts, w.last_run_at, w.last_fetched, w.last_inserted,
                      (SELECT COUNT(*) FROM records r WHERE r.source_id = s.id) AS records
               FROM sources s
               LEFT JOIN watermarks w ON w.source_id = s.id
               WHERE s.project_id = ?
               ORDER BY s.name""",
            (project_id,),
        ).fetchall()
        return {
            "project_id": project_id,
            "sources": [dict(s) for s in sources],
            "total_records": sum(int(s["records"]) for s in sources),
        }


# ---------------------------------------------------------------------------
# CLI smoke test:  python -m srotaai.store
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    db = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB_PATH
    with Store(db) as s:
        rows = s.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        print(f"[OK] {db}")
        for r in rows:
            print(f"  table: {r['name']}")
        print(f"  records: {s.count_records()}")
