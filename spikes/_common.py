"""Shared helpers for spikes. Throwaway."""
from __future__ import annotations
import json, os, time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

# Load .env if present
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

OUT_DIR = Path(__file__).parent.parent / "spike_outputs"
OUT_DIR.mkdir(exist_ok=True)


@dataclass
class RawRecord:
    """Universal shape every connector must yield. This is the abstraction we're testing."""
    external_id: str
    url: Optional[str]
    text: str
    posted_at: datetime
    author_handle: Optional[str] = None
    raw_blob: Optional[str] = None
    language_hint: Optional[str] = None
    source_kind: Optional[str] = None  # debug

    def to_dict(self) -> dict:
        d = asdict(self)
        d["posted_at"] = self.posted_at.isoformat()
        # truncate raw_blob for output sanity
        if self.raw_blob and len(self.raw_blob) > 500:
            d["raw_blob"] = self.raw_blob[:500] + "..."
        if len(self.text) > 500:
            d["text"] = self.text[:500] + "..."
        return d


class BaseConnector:
    """Every connector implements this. The pipeline doesn't care what's underneath."""
    name: str = "base"
    source_kind: str = "unknown"

    def __init__(self, config: dict):
        self.config = config

    def fetch(self, since: Optional[datetime] = None) -> Iterator[RawRecord]:
        raise NotImplementedError

    def health_check(self) -> dict:
        try:
            it = self.fetch(since=datetime(2000, 1, 1, tzinfo=timezone.utc))
            first = next(iter(it), None)
            return {"ok": first is not None, "checked_at": datetime.now(timezone.utc).isoformat()}
        except Exception as e:
            return {"ok": False, "error": str(e), "checked_at": datetime.now(timezone.utc).isoformat()}


def save_findings(spike_name: str, findings: dict) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = OUT_DIR / f"{spike_name}_{ts}.json"
    path.write_text(json.dumps(findings, indent=2, default=str))
    print(f"\n[findings saved] {path}")
    return path


def banner(msg: str):
    print("\n" + "=" * 70)
    print(msg)
    print("=" * 70)


def time_block(label: str):
    """Context-manager-ish timer."""
    class _T:
        def __enter__(self):
            self.t0 = time.time()
            print(f"[{label}] start...")
            return self
        def __exit__(self, *a):
            self.elapsed = time.time() - self.t0
            print(f"[{label}] done in {self.elapsed:.2f}s")
    return _T()
