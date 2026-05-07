"""
Background scheduler — turns each source's `latency` field into actual
periodic re-ingestion. Uses APScheduler's BackgroundScheduler so the
dashboard process keeps a single in-memory loop running.

Cadence map (latency → poll interval):
    real_time → every 5 minutes
    daily     → every 24 hours
    weekly    → every 7 days

Every minute the scheduler:
  1. lists all enabled sources across all projects
  2. for each source, computes (now - watermarks.last_run_at)
  3. if elapsed >= the cadence interval (or no run yet), runs the project

The runner already filters out disabled sources and uses watermarks for
incremental fetch, so re-running a project is cheap when nothing is new.

This is deliberately in-process. Survives Streamlit reruns within one
session but not process restarts; for production swap for cron / k8s
CronJobs invoking `python -m srotaai.scheduler --tick`.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler

from .project import Project
from .runner import run_project
from .store import Store, DEFAULT_DB_PATH

log = logging.getLogger("srotaai.scheduler")

# How long between successive runs for each cadence label.
CADENCE_INTERVAL = {
    "real_time": timedelta(minutes=5),
    "daily":     timedelta(hours=24),
    "weekly":    timedelta(days=7),
}

# How often the tick loop wakes up to re-evaluate due sources.
TICK_INTERVAL_SECONDS = 300

PROJECTS_DIR = Path(__file__).resolve().parent.parent / "projects"


@dataclass
class TickStats:
    """Snapshot of the most recent tick — surfaced on the dashboard."""
    last_tick_at: Optional[datetime] = None
    last_due_count: int = 0
    last_run_count: int = 0
    last_error: Optional[str] = None
    total_ticks: int = 0
    total_runs: int = 0


class SrotaScheduler:
    """Singleton-ish wrapper around APScheduler. One per process."""

    _instance: "SrotaScheduler | None" = None
    _lock = threading.Lock()

    @classmethod
    def get(cls, db_path: Path | None = None) -> "SrotaScheduler":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(db_path or DEFAULT_DB_PATH)
            return cls._instance

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.stats = TickStats()
        self._sched = BackgroundScheduler(daemon=True, timezone="UTC")
        self._run_lock = threading.Lock()
        self._started = False

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self._started:
            return
        self._sched.add_job(
            self._tick, trigger="interval",
            seconds=TICK_INTERVAL_SECONDS,
            id="srota-tick", replace_existing=True,
            max_instances=1, coalesce=True,
        )
        self._sched.start()
        self._started = True
        log.info("scheduler started (tick every %ss)", TICK_INTERVAL_SECONDS)

    def stop(self) -> None:
        if self._started:
            self._sched.shutdown(wait=False)
            self._started = False

    @property
    def running(self) -> bool:
        return self._started

    def tick_now(self) -> dict:
        """Force one tick cycle and return summary (for an Admin button)."""
        return self._tick()

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    def _tick(self) -> dict:
        if not self._run_lock.acquire(blocking=False):
            log.debug("tick skipped — previous tick still running")
            return {"skipped": True}
        try:
            return self._tick_impl()
        finally:
            self._run_lock.release()

    def _tick_impl(self) -> dict:
        now = datetime.now(timezone.utc)
        store = Store(self.db_path)

        # Build a set of project_ids whose status currently allows scheduling.
        # 'running' = due as soon as cadence elapses; 'paused' / 'closed' skip.
        active_pids = {
            r["id"] for r in store.conn.execute(
                "SELECT id FROM projects WHERE status='running'"
            ).fetchall()
        }

        due_by_project: dict[str, list[str]] = {}
        rows = store.conn.execute(
            """SELECT s.project_id, s.name AS src, s.latency,
                      w.last_run_at
               FROM sources s LEFT JOIN watermarks w ON w.source_id = s.id
               WHERE s.enabled = 1"""
        ).fetchall()

        due_count = 0
        for r in rows:
            if r["project_id"] not in active_pids:
                continue
            interval = CADENCE_INTERVAL.get(r["latency"] or "daily")
            if interval is None:
                continue
            last = _parse_iso(r["last_run_at"])
            if last is None or (now - last) >= interval:
                due_by_project.setdefault(r["project_id"], []).append(r["src"])
                due_count += 1

        run_count = 0
        last_error: str | None = None
        for pid, srcs in due_by_project.items():
            yaml_path = PROJECTS_DIR / f"{pid}.yaml"
            if not yaml_path.exists():
                log.warning("project YAML missing for %s — skipping", pid)
                continue
            try:
                project = Project.from_yaml(yaml_path)
                # Disable sources that aren't due yet so we don't re-fetch them.
                for s in project.sources:
                    if s.name not in srcs:
                        s.enabled = False
                store.mark_run_started(pid)
                run_project(project, output_path=None, db_path=self.db_path)
                # Re-score on every tick so PRR refreshes with new records.
                from srotaai import signals as signal_job
                signal_job.run(pid, db_path=self.db_path,
                               sentiment_filter=False, min_n=3)
                store.mark_run_completed(pid)
                run_count += 1
                store.append_audit("scheduler", "tick.run",
                                   target=pid, payload={"sources": srcs})
            except Exception as e:                                  # noqa: BLE001
                log.exception("scheduled run failed for %s", pid)
                last_error = f"{pid}: {e}"

        self.stats.last_tick_at = now
        self.stats.last_due_count = due_count
        self.stats.last_run_count = run_count
        self.stats.last_error = last_error
        self.stats.total_ticks += 1
        self.stats.total_runs += run_count
        log.info("tick: due=%d runs=%d", due_count, run_count)
        return {
            "tick_at": now.isoformat(),
            "due": due_count,
            "ran": run_count,
            "error": last_error,
        }


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:                                             # noqa: BLE001
        return None


# CLI for cron-mode: `python -m srotaai.scheduler --tick`
if __name__ == "__main__":
    import argparse, json
    ap = argparse.ArgumentParser()
    ap.add_argument("--tick", action="store_true",
                    help="run one tick cycle and exit (for cron)")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    sched = SrotaScheduler(args.db)
    if args.tick:
        print(json.dumps(sched.tick_now(), indent=2))
    else:
        sched.start()
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            sched.stop()
