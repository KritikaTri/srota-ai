"""
Spike 1 — Connector abstraction across 4 source types.

Goal: prove that one BaseConnector interface holds:
  (a) Reddit            — official API
  (b) RSS feed          — atom/rss
  (c) FAERS open-data   — bulk dump (sample row simulation; full dump is ~100MB ZIP)
  (d) Headless HTML     — generic web scrape (we use plain HTML page, not Playwright,
                          to keep the spike dependency-free; replace with Playwright in product)

Each connector outputs the same RawRecord. The pipeline-side glue at the bottom
shows how a *single* loop drives all of them.

Run:
    cp .env.example .env   # fill REDDIT_* keys (optional; skipped if missing)
    .venv/bin/pip install -r requirements.txt
    .venv/bin/python spikes/spike1_connectors.py
"""
from __future__ import annotations
import os, io, csv, json, time, zipfile
from datetime import datetime, timezone, timedelta
from typing import Iterator, Optional
from urllib.parse import urlparse

import requests
import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as dtparser

from _common import BaseConnector, RawRecord, save_findings, banner, time_block


# ------------------------------------------------------------------ Reddit
class RedditConnector(BaseConnector):
    name = "reddit"
    source_kind = "api"

    def __init__(self, config):
        super().__init__(config)
        try:
            import praw  # noqa
        except ImportError:
            self._praw = None
            return
        import praw
        cid = os.getenv("REDDIT_CLIENT_ID")
        csec = os.getenv("REDDIT_CLIENT_SECRET")
        ua = os.getenv("REDDIT_USER_AGENT", "srota-spike/0.1")
        if not (cid and csec):
            self._praw = None
            return
        self._praw = praw.Reddit(client_id=cid, client_secret=csec, user_agent=ua)

    def fetch(self, since: Optional[datetime] = None) -> Iterator[RawRecord]:
        if self._praw is None:
            print("  [reddit] skipping — no creds (set REDDIT_CLIENT_ID / SECRET in .env)")
            return
        sub = self.config["subreddit"]
        limit = self.config.get("limit", 25)
        since = since or datetime.now(timezone.utc) - timedelta(days=30)
        for post in self._praw.subreddit(sub).new(limit=limit):
            posted = datetime.fromtimestamp(post.created_utc, tz=timezone.utc)
            if posted <= since:
                continue
            yield RawRecord(
                external_id=f"reddit:{post.id}",
                url=f"https://reddit.com{post.permalink}",
                text=f"{post.title}\n\n{post.selftext or ''}",
                posted_at=posted,
                author_handle=str(post.author),
                raw_blob=json.dumps({"id": post.id, "title": post.title}),
                language_hint="en",
                source_kind=self.source_kind,
            )


# ------------------------------------------------------------------ RSS
class RSSConnector(BaseConnector):
    name = "rss"
    source_kind = "rss"

    def fetch(self, since: Optional[datetime] = None) -> Iterator[RawRecord]:
        feed = feedparser.parse(self.config["feed_url"])
        since = since or datetime.now(timezone.utc) - timedelta(days=365)
        for entry in feed.entries[: self.config.get("limit", 25)]:
            try:
                if hasattr(entry, "published"):
                    posted = dtparser.parse(entry.published)
                elif hasattr(entry, "updated"):
                    posted = dtparser.parse(entry.updated)
                else:
                    posted = datetime.now(timezone.utc)
                if posted.tzinfo is None:
                    posted = posted.replace(tzinfo=timezone.utc)
            except Exception:
                posted = datetime.now(timezone.utc)
            if posted <= since:
                continue
            summary = entry.get("summary", "")
            soup = BeautifulSoup(summary, "lxml")
            yield RawRecord(
                external_id=f"rss:{entry.get('id', entry.link)}",
                url=entry.link,
                text=f"{entry.title}\n\n{soup.get_text(' ', strip=True)}",
                posted_at=posted,
                author_handle=getattr(entry, "author", None),
                raw_blob=json.dumps({"title": entry.title, "link": entry.link})[:500],
                language_hint=feed.feed.get("language", "en"),
                source_kind=self.source_kind,
            )


# ------------------------------------------------------------------ FAERS
class FAERSConnector(BaseConnector):
    """
    FDA FAERS quarterly dumps live at:
       https://fis.fda.gov/extensions/FPD-QDE-FAERS/FPD-QDE-FAERS.html

    Real ingestion downloads a ~100MB ZIP. For the spike, we either:
      - Use a small synthetic CSV row (config: synthetic=True), OR
      - Download a real ZIP if config.zip_url is set.
    """
    name = "faers"
    source_kind = "dump"

    SYNTHETIC = [
        {"primaryid": "100001", "drugname": "METFORMIN", "pt": "lactic acidosis", "event_dt": "2024-11-04", "narrative": "Patient on metformin developed metabolic acidosis."},
        {"primaryid": "100002", "drugname": "METFORMIN", "pt": "nausea", "event_dt": "2024-11-12", "narrative": "Mild nausea reported."},
        {"primaryid": "100003", "drugname": "ATORVASTATIN", "pt": "myalgia", "event_dt": "2024-11-15", "narrative": "Muscle pain after 2 weeks on statin."},
        {"primaryid": "100004", "drugname": "METFORMIN", "pt": "lactic acidosis", "event_dt": "2024-12-01", "narrative": "Severe lactic acidosis; hospitalized."},
    ]

    def fetch(self, since: Optional[datetime] = None) -> Iterator[RawRecord]:
        since = since or datetime(2000, 1, 1, tzinfo=timezone.utc)
        if self.config.get("synthetic", True):
            rows = self.SYNTHETIC
        else:
            rows = self._download_and_parse(self.config["zip_url"])
        for row in rows:
            try:
                posted = dtparser.parse(row["event_dt"]).replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if posted <= since:
                continue
            text = f"Drug: {row.get('drugname','?')}\nReaction: {row.get('pt','?')}\nNarrative: {row.get('narrative','')}"
            yield RawRecord(
                external_id=f"faers:{row['primaryid']}",
                url=None,
                text=text,
                posted_at=posted,
                author_handle="FDA-FAERS",
                raw_blob=json.dumps(row),
                language_hint="en",
                source_kind=self.source_kind,
            )

    @staticmethod
    def _download_and_parse(zip_url: str):
        """Real path. Use only if a small ZIP is available."""
        r = requests.get(zip_url, timeout=60)
        r.raise_for_status()
        z = zipfile.ZipFile(io.BytesIO(r.content))
        # FAERS ZIPs have multiple txt files; this is illustrative only.
        for name in z.namelist():
            if not name.lower().endswith(".txt"):
                continue
            with z.open(name) as f:
                reader = csv.DictReader(io.TextIOWrapper(f, encoding="latin-1"), delimiter="$")
                for row in reader:
                    yield row


# ------------------------------------------------------------------ Generic HTML
class GenericHTMLConnector(BaseConnector):
    """
    Generic HTML scraper using requests + BeautifulSoup + CSS selectors.
    Same shape as the future Playwright-based connector — just simpler runtime.

    config: {url, list_selector, title_selector, body_selector, link_selector, date_selector?, limit}
    """
    name = "generic_html"
    source_kind = "html"

    def fetch(self, since: Optional[datetime] = None) -> Iterator[RawRecord]:
        cfg = self.config
        r = requests.get(cfg["url"], headers={"User-Agent": "srota-spike/0.1"}, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        items = soup.select(cfg["list_selector"])[: cfg.get("limit", 10)]
        if not items:
            print(f"  [generic_html] WARN: list_selector matched 0 items on {cfg['url']}")
        for i, item in enumerate(items):
            t_el = item.select_one(cfg["title_selector"]) if cfg.get("title_selector") else None
            b_el = item.select_one(cfg["body_selector"]) if cfg.get("body_selector") else None
            l_el = item.select_one(cfg["link_selector"]) if cfg.get("link_selector") else None
            title = (t_el.get_text(" ", strip=True) if t_el else "") or ""
            body = (b_el.get_text(" ", strip=True) if b_el else "") or ""
            link = l_el.get("href") if (l_el and l_el.has_attr("href")) else None
            if link and link.startswith("/"):
                p = urlparse(cfg["url"])
                link = f"{p.scheme}://{p.netloc}{link}"
            yield RawRecord(
                external_id=f"html:{cfg['url']}#{i}",
                url=link or cfg["url"],
                text=(title + "\n\n" + body).strip() or "(empty)",
                posted_at=datetime.now(timezone.utc),
                author_handle=None,
                raw_blob=str(item)[:500],
                language_hint="en",
                source_kind=self.source_kind,
            )


# ------------------------------------------------------------------ Driver
REGISTRY = {
    "reddit": RedditConnector,
    "rss": RSSConnector,
    "faers": FAERSConnector,
    "generic_html": GenericHTMLConnector,
}


def build(connector_type: str, config: dict) -> BaseConnector:
    return REGISTRY[connector_type](config)


# What we'll test. Pick robust public sources.
SPIKE_SOURCES = [
    {
        "label": "Reddit r/diabetes",
        "type": "reddit",
        "config": {"subreddit": "diabetes", "limit": 10},
    },
    {
        "label": "FDA Drug Safety RSS",
        "type": "rss",
        "config": {"feed_url": "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/drugs/rss.xml", "limit": 10},
    },
    {
        "label": "FAERS synthetic sample",
        "type": "faers",
        "config": {"synthetic": True},
    },
    {
        "label": "Hacker News (HTML scrape proxy for any forum)",
        "type": "generic_html",
        "config": {
            "url": "https://news.ycombinator.com/",
            "list_selector": "tr.athing",
            "title_selector": "span.titleline > a",
            "body_selector": "span.titleline",
            "link_selector": "span.titleline > a",
            "limit": 10,
        },
    },
]


def main():
    findings = {"sources": [], "summary": {}}
    total_records = 0

    for src in SPIKE_SOURCES:
        banner(f"Spike 1 — {src['label']} ({src['type']})")
        try:
            conn = build(src["type"], src["config"])
        except Exception as e:
            print(f"  build failed: {e}")
            findings["sources"].append({"label": src["label"], "ok": False, "error": str(e)})
            continue

        records = []
        err = None
        with time_block(src["label"]) as t:
            try:
                for rec in conn.fetch():
                    records.append(rec)
            except Exception as e:
                err = repr(e)
                print(f"  ERROR: {err}")

        elapsed = getattr(t, "elapsed", 0.0)
        ok = err is None and len(records) > 0
        print(f"  records: {len(records)}  ok={ok}  err={err}")
        if records:
            print(f"  sample[0].text[:120]: {records[0].text[:120]!r}")

        total_records += len(records)
        findings["sources"].append({
            "label": src["label"],
            "type": src["type"],
            "ok": ok,
            "n_records": len(records),
            "elapsed_sec": round(elapsed, 2),
            "rate_records_per_sec": round(len(records) / elapsed, 2) if elapsed else None,
            "error": err,
            "sample": records[0].to_dict() if records else None,
        })

    findings["summary"] = {
        "total_records": total_records,
        "sources_tested": len(SPIKE_SOURCES),
        "sources_ok": sum(1 for s in findings["sources"] if s.get("ok")),
        "abstraction_holds": all(  # every connector returned RawRecord shape
            s.get("ok") in (True, False) for s in findings["sources"]
        ),
    }
    save_findings("spike1_connectors", findings)
    banner("DONE — Spike 1")
    print(json.dumps(findings["summary"], indent=2))


if __name__ == "__main__":
    main()
