"""Time-window value object — what *period* a fetch covers (vs. latency, which is *how often*)."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta


@dataclass
class TimeWindow:
    """
    Either a sliding lookback (`lookback_days`) or an absolute [since, until] range.
    Resolves to (since_utc, until_utc) at runtime.
    """
    lookback_days: int | None = 7
    since: datetime | None = None
    until: datetime | None = None

    def resolve(self, now: datetime | None = None) -> tuple[datetime, datetime]:
        now = now or datetime.now(timezone.utc)
        if self.since and self.until:
            return self.since, self.until
        if self.since:
            return self.since, now
        if self.lookback_days is not None:
            return now - timedelta(days=self.lookback_days), now
        # Default: last 7 days
        return now - timedelta(days=7), now

    def to_dict(self) -> dict:
        return {
            "lookback_days": self.lookback_days,
            "since": self.since.isoformat() if self.since else None,
            "until": self.until.isoformat() if self.until else None,
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "TimeWindow":
        if not d:
            return cls()
        def _dt(v):
            if v is None or isinstance(v, datetime):
                return v
            return datetime.fromisoformat(str(v))
        return cls(
            lookback_days=d.get("lookback_days"),
            since=_dt(d.get("since")),
            until=_dt(d.get("until")),
        )
