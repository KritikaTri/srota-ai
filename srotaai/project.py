"""
Project model for SrotaAI.

A *Project* is the top-level configuration unit demanded by the spec:
  - keywords to monitor (drugs, symptoms, conditions)
  - sources to monitor them on (Reddit, RSS, HTML, FAERS, ...)
  - latency per source (how often the scheduler fires)
  - time_window (which period of history each fetch covers)

Loaded from YAML; the admin UI will eventually write this YAML.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any
import json
import yaml

from .timewindow import TimeWindow


class Latency(str, Enum):
    REAL_TIME = "real_time"   # poll every ~5 min
    DAILY = "daily"
    WEEKLY = "weekly"

    @property
    def seconds(self) -> int:
        return {"real_time": 300, "daily": 86_400, "weekly": 604_800}[self.value]


@dataclass
class SourceConfig:
    """One monitored source within a project."""
    name: str
    connector: str                                # reddit | rss | html | faers
    params: dict[str, Any] = field(default_factory=dict)
    latency: Latency = Latency.DAILY
    time_window: TimeWindow | None = None         # None -> inherit from project
    time_windows: list[TimeWindow] | None = None  # multiple disjoint windows; takes priority
    enabled: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.latency, str):
            self.latency = Latency(self.latency)
        if isinstance(self.time_window, dict):
            self.time_window = TimeWindow.from_dict(self.time_window)
        if self.time_windows is not None:
            self.time_windows = [
                w if isinstance(w, TimeWindow) else TimeWindow.from_dict(w)
                for w in self.time_windows
            ]

    def effective_windows(self, project_default: "TimeWindow",
                          project_default_list: list[TimeWindow] | None) -> list[TimeWindow]:
        """Resolution order: source.time_windows > source.time_window
        > project.time_windows > project.time_window."""
        if self.time_windows:
            return self.time_windows
        if self.time_window:
            return [self.time_window]
        if project_default_list:
            return project_default_list
        return [project_default]


@dataclass
class Project:
    """A social-monitoring project — keywords + sources + cadence + window(s)."""
    id: str
    name: str
    keywords: list[str]
    sources: list[SourceConfig]
    description: str = ""
    time_window: TimeWindow = field(default_factory=lambda: TimeWindow(lookback_days=7))
    time_windows: list[TimeWindow] | None = None  # optional list of disjoint windows

    def __post_init__(self) -> None:
        if isinstance(self.time_window, dict):
            self.time_window = TimeWindow.from_dict(self.time_window)
        if self.time_windows is not None:
            self.time_windows = [
                w if isinstance(w, TimeWindow) else TimeWindow.from_dict(w)
                for w in self.time_windows
            ]

    def effective_window(self, source: SourceConfig) -> TimeWindow:
        """Back-compat: returns the FIRST effective window (single)."""
        return self.effective_windows(source)[0]

    def effective_windows(self, source: SourceConfig) -> list[TimeWindow]:
        """All windows the runner should iterate for this source."""
        return source.effective_windows(self.time_window, self.time_windows)

    # ---- IO -----------------------------------------------------------
    @classmethod
    def from_yaml(cls, path: str | Path) -> "Project":
        data = yaml.safe_load(Path(path).read_text())
        sources_raw = data.pop("sources", [])
        sources = [SourceConfig(**s) for s in sources_raw]
        tw = data.pop("time_window", None)
        tws = data.pop("time_windows", None)
        kwargs = {**data, "sources": sources}
        if tw is not None:
            kwargs["time_window"] = TimeWindow.from_dict(tw)
        if tws is not None:
            kwargs["time_windows"] = [TimeWindow.from_dict(w) for w in tws]
        return cls(**kwargs)

    def to_yaml(self, path: str | Path) -> None:
        Path(path).write_text(yaml.safe_dump(self._serialisable(), sort_keys=False))

    def to_json(self) -> str:
        return json.dumps(self._serialisable(), indent=2, default=str)

    def _serialisable(self) -> dict:
        d = asdict(self)
        d["time_window"] = self.time_window.to_dict()
        d["time_windows"] = (
            [w.to_dict() for w in self.time_windows] if self.time_windows else None
        )
        for src, src_obj in zip(d["sources"], self.sources):
            src["latency"] = src_obj.latency.value
            src["time_window"] = src_obj.time_window.to_dict() if src_obj.time_window else None
            src["time_windows"] = (
                [w.to_dict() for w in src_obj.time_windows] if src_obj.time_windows else None
            )
        return d


# ----------------------------------------------------------------------
# Demo / smoke test
# ----------------------------------------------------------------------
if __name__ == "__main__":
    proj = Project(
        id="pv-india-otc",
        name="India OTC Pharmacovigilance",
        description="Monitor common Indian OTC drugs for adverse reactions.",
        time_window=TimeWindow(lookback_days=14),  # project default: 14 days
        keywords=[
            "Crocin", "Dolo", "Telma", "Ecosprin",
            "rash", "lactic acidosis", "myalgia",
        ],
        sources=[
            SourceConfig(
                name="reddit-r-india",
                connector="reddit",
                params={"subreddits": ["india", "AskDocs"], "limit": 200},
                latency=Latency.REAL_TIME,
                time_window=TimeWindow(lookback_days=3),  # tighter for real-time
            ),
            SourceConfig(
                name="fda-recalls-rss",
                connector="rss",
                params={"feed_url": "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/drugs/rss.xml"},
                latency=Latency.DAILY,
            ),
            SourceConfig(
                name="faers-synthetic",
                connector="faers",
                params={"synthetic": True},
                latency=Latency.WEEKLY,
                time_window=TimeWindow(lookback_days=900),  # FAERS sample is from late 2024
            ),
            SourceConfig(
                name="1mg-reviews",
                connector="html",
                params={
                    "url": "https://www.1mg.com/drugs/crocin-advance-tablet-130257",
                    "list_selector": "div.review-item",
                    "title_selector": "p.review-text",
                    "body_selector": "p.review-text",
                    "link_selector": "a",
                },
                latency=Latency.DAILY,
                enabled=False,  # gated behind R2 stealth fetcher
            ),
        ],
    )

    out_dir = Path(__file__).parent.parent / "projects"
    out_dir.mkdir(exist_ok=True)
    yaml_path = out_dir / "pv-india-otc.yaml"
    proj.to_yaml(yaml_path)

    loaded = Project.from_yaml(yaml_path)
    assert loaded.id == proj.id
    assert loaded.time_window.lookback_days == 14
    assert loaded.sources[0].time_window.lookback_days == 3

    print(f"[OK] Wrote {yaml_path}")
    print(f"[OK] Round-trip OK — {loaded.name}")
    print(f"[OK] Project window: lookback_days={loaded.time_window.lookback_days}")
    print(f"[OK] {len(loaded.keywords)} keywords, {len(loaded.sources)} sources:")
    for s in loaded.sources:
        flag = "" if s.enabled else " (disabled)"
        win = loaded.effective_window(s)
        since, until = win.resolve()
        span_d = (until - since).days
        print(f"     - {s.name:22s} {s.connector:6s} @ {s.latency.value:9s} window={span_d}d{flag}")
