"""
Extra connectors for SrotaAI Day 2:
  - HtmlStealthConnector  : retry-ladder fetch + CSS selectors
  - XStubConnector        : twitterapi.io if creds present, else replay fixture
                            (drive demo without paying for X API)

Both implement the spike1 BaseConnector contract and are auto-registered into
the spike1 REGISTRY when this module is imported.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterator, Optional

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "spikes"))
from _common import BaseConnector, RawRecord  # noqa: E402

from .fetch import fetch as ladder_fetch  # noqa: E402


# ---------------------------------------------------------------------------
# HTML (stealth)
# ---------------------------------------------------------------------------
class HtmlStealthConnector(BaseConnector):
    """
    Generic HTML scraper that uses srotaai.fetch's retry ladder.
    Config:
        url               required
        list_selector     CSS for the repeated review/post element
        title_selector    optional CSS within each item
        body_selector     CSS for the text body
        link_selector     optional CSS for an <a> with href
        date_selector     optional CSS for a date element
        max_items         default 100
    """
    name = "html_stealth"
    source_kind = "html"

    def fetch(self, since: Optional[datetime] = None) -> Iterator[RawRecord]:
        url = self.config["url"]
        list_sel = self.config.get("list_selector")
        body_sel = self.config.get("body_selector") or list_sel
        title_sel = self.config.get("title_selector")
        link_sel = self.config.get("link_selector")
        date_sel = self.config.get("date_selector")
        max_items = int(self.config.get("max_items", 100))

        res = ladder_fetch(url)
        if not res.ok:
            print(f"  [html_stealth] {url} — all rungs failed "
                  f"(attempts={[a.get('rung') for a in res.attempts]})")
            return
        print(f"  [html_stealth] {url} ✓ rung={res.rung} status={res.status} "
              f"({res.elapsed_s}s)")

        soup = BeautifulSoup(res.text, "lxml")
        items = soup.select(list_sel) if list_sel else [soup]
        for i, item in enumerate(items[:max_items]):
            body = ""
            if body_sel:
                el = item.select_one(body_sel)
                body = el.get_text(" ", strip=True) if el else ""
            title = ""
            if title_sel:
                el = item.select_one(title_sel)
                title = el.get_text(" ", strip=True) if el else ""
            text = (title + "\n\n" + body).strip() if title else body
            if not text:
                continue

            link = url
            if link_sel:
                a = item.select_one(link_sel)
                if a and a.get("href"):
                    href = a["href"]
                    link = href if href.startswith("http") else url.rsplit("/", 1)[0] + "/" + href.lstrip("/")

            posted = None
            if date_sel:
                el = item.select_one(date_sel)
                if el:
                    try:
                        posted = dtparser.parse(el.get_text(strip=True))
                        if posted.tzinfo is None:
                            posted = posted.replace(tzinfo=timezone.utc)
                    except Exception:                     # noqa: BLE001
                        posted = None
            if posted is None:
                posted = datetime.now(timezone.utc)

            ext = "html:" + hashlib.sha1(f"{link}#{i}".encode()).hexdigest()[:16]
            yield RawRecord(
                external_id=ext,
                url=link,
                text=text,
                posted_at=posted,
                language_hint=None,
                source_kind=self.source_kind,
                raw_blob=json.dumps({"rung": res.rung}),
            )


# ---------------------------------------------------------------------------
# HTML (auto-extract) — no CSS selectors required
# ---------------------------------------------------------------------------
class HtmlAutoConnector(BaseConnector):
    """
    Fetches a URL and auto-extracts substantive text blocks.
    Heuristics:
      * strip <script>/<style>/<nav>/<header>/<footer>/<aside>/<form>
      * collect text from <article>, <p>, <li>, <blockquote>
      * keep blocks with >= min_chars characters (default 80)
      * de-duplicate on first 200 chars

    Config:
        url         required
        min_chars   default 80
        max_items   default 200
    """
    name = "html_auto"
    source_kind = "html"

    def fetch(self, since: Optional[datetime] = None) -> Iterator[RawRecord]:
        url = self.config["url"]
        min_chars = int(self.config.get("min_chars", 80))
        max_items = int(self.config.get("max_items", 200))

        res = ladder_fetch(url)
        if not res.ok:
            print(f"  [html_auto] {url} — all rungs failed "
                  f"(attempts={[a.get('rung') for a in res.attempts]})")
            return
        print(f"  [html_auto] {url} ✓ rung={res.rung} status={res.status} "
              f"({res.elapsed_s}s)")

        soup = BeautifulSoup(res.text, "lxml")
        for tag in soup(["script", "style", "nav", "header", "footer",
                         "aside", "form", "noscript"]):
            tag.decompose()

        page_title = (soup.title.string.strip()
                      if soup.title and soup.title.string else "")

        seen: set[str] = set()
        items: list[str] = []
        for el in soup.select("article, p, li, blockquote"):
            text = el.get_text(" ", strip=True)
            if len(text) < min_chars:
                continue
            key = text[:200]
            if key in seen:
                continue
            seen.add(key)
            items.append(text)
            if len(items) >= max_items:
                break

        if not items:
            print(f"  [html_auto] {url} — no substantive text blocks found")
            return

        now = datetime.now(timezone.utc)
        for i, text in enumerate(items):
            ext = "html:" + hashlib.sha1(f"{url}#{i}#{text[:80]}".encode()
                                          ).hexdigest()[:16]
            yield RawRecord(
                external_id=ext,
                url=url,
                text=(page_title + "\n\n" + text) if page_title else text,
                posted_at=now,
                language_hint=None,
                source_kind=self.source_kind,
                raw_blob=json.dumps({"rung": res.rung, "block": i}),
            )


# ---------------------------------------------------------------------------
# HTML listing crawler — discover URLs from a seed page or sitemap.xml,
# then run the auto-extractor on each.
# ---------------------------------------------------------------------------
class HtmlListingConnector(BaseConnector):
    """
    Crawls a seed (a regular page OR a sitemap.xml) to discover URLs, then
    fetches each one and extracts text using the same heuristics as
    HtmlAutoConnector.

    Modes:
      Mode A — seed + link_pattern:
          seed_url = "https://www.1mg.com/categories/pain-relief"
          link_pattern = r"^/drugs/[a-z0-9-]+-\\d+$"   # regex on href
      Mode B — sitemap:
          seed_url = "https://example.com/sitemap.xml"
          (auto-detected by .xml suffix or content-type)

    Config:
        seed_url       required
        link_pattern   regex applied to discovered hrefs (Mode A only)
        max_urls       default 50  — politeness cap
        sleep_seconds  default 1.0 — pause between fetches
        min_chars      default 80
        same_host_only default True
    """
    name = "html_listing"
    source_kind = "html"

    def fetch(self, since: Optional[datetime] = None) -> Iterator[RawRecord]:
        seed = self.config["seed_url"]
        link_pat = self.config.get("link_pattern")
        max_urls = int(self.config.get("max_urls", 50))
        sleep_s = float(self.config.get("sleep_seconds", 1.0))
        min_chars = int(self.config.get("min_chars", 80))
        same_host = bool(self.config.get("same_host_only", True))

        from urllib.parse import urlparse, urljoin
        seed_host = urlparse(seed).netloc

        # ---- 1) discover URLs ------------------------------------------
        urls = self._discover(seed, link_pat, max_urls, same_host)
        if not urls:
            print(f"  [html_listing] {seed} — discovered 0 URLs")
            return
        print(f"  [html_listing] {seed} → {len(urls)} URLs to fetch")

        # ---- 2) fetch each one with the auto-extractor logic -----------
        import time
        for u in urls:
            res = ladder_fetch(u)
            if not res.ok:
                print(f"    [html_listing] FAIL {u}")
                time.sleep(sleep_s)
                continue
            soup = BeautifulSoup(res.text, "lxml")
            for tag in soup(["script", "style", "nav", "header", "footer",
                             "aside", "form", "noscript"]):
                tag.decompose()
            page_title = (soup.title.string.strip()
                          if soup.title and soup.title.string else "")
            seen: set[str] = set()
            blocks = []
            for el in soup.select("article, p, li, blockquote"):
                text = el.get_text(" ", strip=True)
                if len(text) < min_chars:
                    continue
                k = text[:200]
                if k in seen:
                    continue
                seen.add(k)
                blocks.append(text)
                if len(blocks) >= 200:
                    break
            now = datetime.now(timezone.utc)
            for i, text in enumerate(blocks):
                ext = "html:" + hashlib.sha1(
                    f"{u}#{i}#{text[:80]}".encode()).hexdigest()[:16]
                yield RawRecord(
                    external_id=ext,
                    url=u,
                    text=(page_title + "\n\n" + text) if page_title else text,
                    posted_at=now,
                    language_hint=None,
                    source_kind=self.source_kind,
                    raw_blob=json.dumps({"page_url": u, "block": i,
                                         "rung": res.rung}),
                )
            time.sleep(sleep_s)

    # -------- discovery --------
    def _discover(self, seed: str, link_pat: str | None,
                  max_urls: int, same_host: bool) -> list[str]:
        from urllib.parse import urlparse, urljoin
        seed_host = urlparse(seed).netloc

        res = ladder_fetch(seed)
        if not res.ok:
            print(f"  [html_listing] could not fetch seed: {seed}")
            return []

        # sitemap?
        looks_like_sitemap = (seed.lower().endswith(".xml") or
                              "<urlset" in res.text[:2000].lower() or
                              "<sitemapindex" in res.text[:2000].lower())
        if looks_like_sitemap:
            return self._parse_sitemap(res.text, max_urls,
                                        seed_host if same_host else None)

        # seed + pattern
        soup = BeautifulSoup(res.text, "lxml")
        regex = re.compile(link_pat) if link_pat else None
        out: list[str] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            absolute = urljoin(seed, href)
            host = urlparse(absolute).netloc
            if same_host and host != seed_host:
                continue
            path = urlparse(absolute).path
            if regex and not regex.search(path):
                continue
            if absolute in seen:
                continue
            seen.add(absolute)
            out.append(absolute)
            if len(out) >= max_urls:
                break
        return out

    @staticmethod
    def _parse_sitemap(xml_text: str, max_urls: int,
                       same_host: str | None) -> list[str]:
        from urllib.parse import urlparse
        soup = BeautifulSoup(xml_text, "xml")
        out: list[str] = []
        for loc in soup.find_all("loc"):
            u = loc.get_text(strip=True)
            if not u:
                continue
            if same_host and urlparse(u).netloc != same_host:
                continue
            out.append(u)
            if len(out) >= max_urls:
                break
        return out


# ---------------------------------------------------------------------------
# openFDA — live FAERS via api.fda.gov (no API key needed for low volume)
# ---------------------------------------------------------------------------
OPENFDA_BASE = "https://api.fda.gov/drug/event.json"


class OpenFdaConnector(BaseConnector):
    """
    Pulls real FAERS adverse-event reports from openFDA. No auth; rate limit
    is 240/min/IP without a key, 240/min after `OPENFDA_API_KEY` env var
    is set (with much higher daily ceiling).

    Config:
        search       FDA query string (e.g. 'patient.drug.medicinalproduct:"METFORMIN"')
        limit        total records to pull (default 1000, max 26000)
        per_page     records per HTTP call (default 100, max 100)
    """
    name = "openfda"
    source_kind = "faers"

    def fetch(self, since: Optional[datetime] = None) -> Iterator[RawRecord]:
        search = self.config.get("search", "")
        total = int(self.config.get("limit", 1000))
        per_page = min(int(self.config.get("per_page", 100)), 100)
        sort_by = self.config.get("sort", "receivedate:desc")
        api_key = os.getenv("OPENFDA_API_KEY")

        skip = 0
        fetched = 0
        while fetched < total:
            params = {"limit": min(per_page, total - fetched), "skip": skip}
            if search:
                params["search"] = search
            if sort_by:
                params["sort"] = sort_by
            if api_key:
                params["api_key"] = api_key
            try:
                r = requests.get(OPENFDA_BASE, params=params, timeout=20)
                if r.status_code == 404:  # past end of results
                    break
                r.raise_for_status()
                data = r.json()
            except Exception as e:                           # noqa: BLE001
                print(f"  [openfda] fetch failed at skip={skip}: {e}")
                return
            results = data.get("results") or []
            if not results:
                break
            print(f"  [openfda] skip={skip} got={len(results)}")
            for rec in results:
                rr = self._to_raw(rec)
                if rr is not None:
                    yield rr
            fetched += len(results)
            skip += len(results)
            if len(results) < per_page:
                break

    @staticmethod
    def _to_raw(rec: dict) -> Optional[RawRecord]:
        report_id = rec.get("safetyreportid") or rec.get("safetyreportversion")
        if not report_id:
            return None
        patient = rec.get("patient") or {}
        drugs = [d.get("medicinalproduct") for d in (patient.get("drug") or [])
                 if d.get("medicinalproduct")]
        reactions = [r.get("reactionmeddrapt")
                     for r in (patient.get("reaction") or [])
                     if r.get("reactionmeddrapt")]
        if not drugs and not reactions:
            return None
        # Build a synthetic narrative so keyword filter + NER both work.
        text = (
            f"FAERS report {report_id}. "
            f"Drugs: {', '.join(drugs) or 'unspecified'}. "
            f"Adverse events: {', '.join(reactions) or 'unspecified'}. "
            f"Country: {rec.get('occurcountry', 'unknown')}. "
            f"Outcome serious: {rec.get('serious', 'unknown')}."
        )
        date_str = rec.get("receiptdate") or rec.get("transmissiondate") or ""
        try:
            posted = (datetime.strptime(date_str, "%Y%m%d")
                      .replace(tzinfo=timezone.utc))
        except Exception:                                     # noqa: BLE001
            posted = datetime.now(timezone.utc)
        return RawRecord(
            external_id=f"openfda:{report_id}",
            url=f"https://api.fda.gov/drug/event.json?search=safetyreportid:{report_id}",
            text=text,
            posted_at=posted,
            language_hint="en",
            source_kind="faers",
            raw_blob=json.dumps({"drugs": drugs, "reactions": reactions,
                                  "country": rec.get("occurcountry")})[:2000],
        )


# ---------------------------------------------------------------------------
# WhatsApp Business — fixture replay for demo, webhook-fed JSON for production
# ---------------------------------------------------------------------------
WA_DEFAULT_FIXTURE = (Path(__file__).resolve().parent.parent
                      / "data" / "whatsapp_fixture.json")


class WhatsAppConnector(BaseConnector):
    """
    Reads inbound WhatsApp messages from a JSON file. The file is either:
      * a hand-curated fixture (default: data/whatsapp_fixture.json), or
      * a live file that `srotaai/whatsapp_webhook.py` appends to as Meta
        Cloud API delivers webhooks.

    Schema per entry:
        {"id": "...", "from": "+91...", "ts": ISO-8601,
         "lang": "en"|"hi", "text": "..."}

    PII (phone numbers, names) is redacted by the runner's PII pipeline
    *after* this connector returns the raw text — same as every source.

    Config:
        path     defaults to data/whatsapp_fixture.json
        shortcode optional label (e.g. "+91 14416") added to raw_blob
    """
    name = "whatsapp"
    source_kind = "whatsapp"

    def fetch(self, since: Optional[datetime] = None) -> Iterator[RawRecord]:
        path = Path(self.config.get("path") or WA_DEFAULT_FIXTURE)
        shortcode = self.config.get("shortcode")
        if not path.exists():
            print(f"  [whatsapp] inbox file not found: {path}")
            return
        try:
            items = json.loads(path.read_text())
        except Exception as e:                                # noqa: BLE001
            print(f"  [whatsapp] could not parse {path}: {e}")
            return
        for m in items:
            posted = self._parse_dt(m.get("ts")) or datetime.now(timezone.utc)
            if since and posted <= since:
                continue
            text = m.get("text") or ""
            if not text.strip():
                continue
            yield RawRecord(
                external_id=f"wa:{m.get('id') or hashlib.sha1(text.encode()).hexdigest()[:12]}",
                url=f"whatsapp://message/{m.get('id', '')}",
                text=text,
                posted_at=posted,
                author_handle=m.get("from"),
                language_hint=m.get("lang", "en"),
                source_kind=self.source_kind,
                raw_blob=json.dumps({"shortcode": shortcode, **m})[:2000],
            )

    @staticmethod
    def _parse_dt(s) -> Optional[datetime]:
        if not s:
            return None
        try:
            d = dtparser.parse(s)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except Exception:                                     # noqa: BLE001
            return None


# ---------------------------------------------------------------------------
# Reddit (PRAW if creds, else anonymous JSON; always pulls comments)
# ---------------------------------------------------------------------------
REDDIT_UA = os.getenv("REDDIT_USER_AGENT",
                      "srotaai/0.2 (pharmacovigilance research)")

# Env-controlled ingestion caps. Today (anonymous): keep these conservative
# to stay under Reddit's anonymous rate-limit tolerance. When you register
# a Reddit script app and add REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET to .env,
# you can raise these (e.g. REDDIT_MAX_RECORDS=10000) without code changes.
REDDIT_MAX_RECORDS_PER_SOURCE = int(os.getenv("REDDIT_MAX_RECORDS", "1000"))
REDDIT_MAX_PAGES_PER_SUB      = int(os.getenv("REDDIT_MAX_PAGES", "5"))
REDDIT_INTER_SUB_SLEEP_SEC    = float(os.getenv("REDDIT_INTER_SUB_SLEEP", "1.0"))


class RedditPlusConnector(BaseConnector):
    """
    Reddit ingestion that:
      * uses PRAW if REDDIT_CLIENT_ID/SECRET are set (authenticated, higher rate limit)
      * falls back to anonymous reddit.com .json endpoints otherwise
      * for each post, also fetches the comment tree and yields
        each comment as its own RawRecord (with parent_id linkage)

    Config:
        subreddit   required (e.g. "AskDocs")
        query       optional — if set, hits /r/{sub}/search.json instead of /new
        limit       default 25 posts
        with_comments  default True
        max_comments_per_post  default 100
    """
    name = "reddit_plus"
    source_kind = "api"

    def __init__(self, config):
        super().__init__(config)
        self._praw = None
        cid = os.getenv("REDDIT_CLIENT_ID")
        csec = os.getenv("REDDIT_CLIENT_SECRET")
        if cid and csec:
            try:
                import praw  # noqa: WPS433
                self._praw = praw.Reddit(
                    client_id=cid, client_secret=csec,
                    user_agent=REDDIT_UA,
                )
            except Exception:                                  # noqa: BLE001
                self._praw = None

    # -------- public --------
    def fetch(self, since: Optional[datetime] = None) -> Iterator[RawRecord]:
        # Accept either "subreddit" (single str) or "subreddits" (list).
        subs = self.config.get("subreddits")
        if not subs:
            single = self.config.get("subreddit")
            subs = [single] if single else ["all"]
        if isinstance(subs, str):
            subs = [subs]
        subs = [s.strip().lstrip("r/") for s in subs if s and s.strip()]

        q = self.config.get("query")
        # Per-source hard cap (env-overridable). Each sub gets a share of this.
        total_cap = min(
            int(self.config.get("limit", REDDIT_MAX_RECORDS_PER_SOURCE)),
            REDDIT_MAX_RECORDS_PER_SOURCE,
        )
        per_sub_cap = max(1, total_cap // max(1, len(subs)))
        with_comments = bool(self.config.get("with_comments", True))
        max_comments = int(self.config.get("max_comments_per_post", 50))
        since = since or datetime.now(timezone.utc) - timedelta(days=30)

        emitted = 0
        import time as _time
        for sub_idx, sub in enumerate(subs):
            if emitted >= total_cap:
                break
            remaining = total_cap - emitted
            sub_budget = min(per_sub_cap, remaining)
            if self._praw:
                print(f"  [reddit] r/{sub} via PRAW budget={sub_budget}")
                posts_iter = self._iter_posts_praw(sub, q, sub_budget)
            else:
                print(f"  [reddit] r/{sub} via anon JSON budget={sub_budget} (no creds)")
                posts_iter = self._iter_posts_anon(sub, q, sub_budget)

            for post in posts_iter:
                if emitted >= total_cap:
                    break
                if post["posted_at"] <= since:
                    continue
                yield RawRecord(
                    external_id=f"reddit:{post['id']}",
                    url=post["url"],
                    text=f"{post['title']}\n\n{post.get('body') or ''}".strip(),
                    posted_at=post["posted_at"],
                    author_handle=post.get("author"),
                    language_hint="en",
                    source_kind=self.source_kind,
                    raw_blob=json.dumps({"kind": "post",
                                         "id": post["id"],
                                         "subreddit": sub})[:2000],
                )
                emitted += 1

                if with_comments and emitted < total_cap:
                    for c in self._iter_comments(post["id"], sub, max_comments):
                        if emitted >= total_cap:
                            break
                        if c["posted_at"] <= since:
                            continue
                        yield RawRecord(
                            external_id=f"reddit:{post['id']}:c:{c['id']}",
                            url=post["url"] + c["id"] + "/",
                            text=c["body"],
                            posted_at=c["posted_at"],
                            author_handle=c.get("author"),
                            language_hint="en",
                            source_kind=self.source_kind,
                            raw_blob=json.dumps({
                                "kind": "comment",
                                "post_id": post["id"],
                                "comment_id": c["id"],
                                "parent_id": c.get("parent_id"),
                                "subreddit": sub,
                            })[:2000],
                        )
                        emitted += 1

            # Polite delay between subs to stay under anon rate-limit.
            if sub_idx < len(subs) - 1 and not self._praw:
                _time.sleep(REDDIT_INTER_SUB_SLEEP_SEC)

        print(f"  [reddit] total emitted from {len(subs)} sub(s): {emitted}")

    # -------- PRAW path --------
    def _iter_posts_praw(self, sub: str, query: str | None, limit: int):
        sr = self._praw.subreddit(sub)
        seq = (sr.search(query, sort="new", limit=limit) if query
               else sr.new(limit=limit))
        for p in seq:
            yield {
                "id": p.id,
                "title": p.title,
                "body": p.selftext or "",
                "url": f"https://www.reddit.com{p.permalink}",
                "author": str(p.author) if p.author else None,
                "posted_at": datetime.fromtimestamp(p.created_utc,
                                                    tz=timezone.utc),
            }

    # -------- anonymous JSON path with after= pagination --------
    def _iter_posts_anon(self, sub: str, query: str | None, limit: int):
        """Page through Reddit's anonymous .json endpoints using after= cursor.

        Caps at REDDIT_MAX_PAGES_PER_SUB pages (each up to 100 posts) to
        stay well under the anon rate-limit envelope.
        """
        per_page = min(100, limit)
        page = 0
        emitted = 0
        after: str | None = None
        while emitted < limit and page < REDDIT_MAX_PAGES_PER_SUB:
            if query:
                url = f"https://www.reddit.com/r/{sub}/search.json"
                params = {"q": query, "sort": "new", "limit": per_page,
                          "restrict_sr": "true"}
            else:
                url = f"https://www.reddit.com/r/{sub}/new.json"
                params = {"limit": per_page}
            if after:
                params["after"] = after
            try:
                r = requests.get(url, params=params,
                                 headers={"User-Agent": REDDIT_UA}, timeout=20)
                if r.status_code == 429:
                    print(f"  [reddit] anon 429 throttle on r/{sub} page {page}; stopping.")
                    return
                r.raise_for_status()
                payload = r.json().get("data", {}) or {}
                children = payload.get("children", []) or []
                after = payload.get("after")
            except Exception as e:                                  # noqa: BLE001
                print(f"  [reddit] anon fetch failed on r/{sub} page {page}: {e}")
                return
            if not children:
                return
            for ch in children:
                if emitted >= limit:
                    return
                d = ch.get("data") or {}
                if not d.get("id"):
                    continue
                yield {
                    "id": d["id"],
                    "title": d.get("title", ""),
                    "body": d.get("selftext", ""),
                    "url": "https://www.reddit.com" + d.get("permalink", ""),
                    "author": d.get("author"),
                    "posted_at": datetime.fromtimestamp(d.get("created_utc") or 0,
                                                        tz=timezone.utc),
                }
                emitted += 1
            page += 1
            if not after:
                return

    # -------- comments (PRAW or anon) --------
    def _iter_comments(self, post_id: str, sub: str, max_comments: int):
        if self._praw:
            try:
                submission = self._praw.submission(id=post_id)
                submission.comments.replace_more(limit=0)
                for c in submission.comments.list()[:max_comments]:
                    yield {
                        "id": c.id,
                        "body": c.body,
                        "author": str(c.author) if c.author else None,
                        "parent_id": c.parent_id,
                        "posted_at": datetime.fromtimestamp(
                            c.created_utc, tz=timezone.utc),
                    }
                return
            except Exception as e:                              # noqa: BLE001
                print(f"  [reddit] PRAW comments failed for {post_id}: {e}")
                # fall through to anon

        url = f"https://www.reddit.com/r/{sub}/comments/{post_id}.json"
        try:
            r = requests.get(url, params={"limit": max_comments},
                             headers={"User-Agent": REDDIT_UA}, timeout=15)
            r.raise_for_status()
            data = r.json()
        except Exception as e:                                   # noqa: BLE001
            print(f"  [reddit] anon comments failed for {post_id}: {e}")
            return
        if not isinstance(data, list) or len(data) < 2:
            return
        # Walk the tree depth-first, flattening.
        stack = list((data[1].get("data") or {}).get("children") or [])
        n = 0
        while stack and n < max_comments:
            node = stack.pop(0)
            if node.get("kind") != "t1":
                continue
            d = node.get("data") or {}
            if not d.get("id") or not d.get("body"):
                continue
            yield {
                "id": d["id"],
                "body": d["body"],
                "author": d.get("author"),
                "parent_id": d.get("parent_id"),
                "posted_at": datetime.fromtimestamp(d.get("created_utc") or 0,
                                                    tz=timezone.utc),
            }
            n += 1
            replies = d.get("replies")
            if isinstance(replies, dict):
                stack.extend((replies.get("data") or {}).get("children") or [])


# ---------------------------------------------------------------------------
# X / Twitter (stub with optional twitterapi.io)
# ---------------------------------------------------------------------------
DEFAULT_FIXTURE = Path(__file__).resolve().parent.parent / "data" / "x_fixture.json"


class XStubConnector(BaseConnector):
    """
    If TWITTERAPI_IO_KEY is set, queries https://api.twitterapi.io/twitter/tweet/advanced_search.
    Otherwise reads a JSON fixture so the demo runs offline.

    Config:
        query        e.g. "Crocin OR Dolo lang:en"
        max_results  default 50
        fixture_path override fixture (defaults to data/x_fixture.json)
    """
    name = "x_stub"
    source_kind = "x"

    def fetch(self, since: Optional[datetime] = None) -> Iterator[RawRecord]:
        query = self.config.get("query", "")
        max_results = int(self.config.get("max_results", 50))
        api_key = os.getenv("TWITTERAPI_IO_KEY")

        if api_key and query:
            yield from self._fetch_live(query, api_key, max_results, since)
        else:
            print("  [x_stub] no TWITTERAPI_IO_KEY — replaying fixture")
            yield from self._fetch_fixture(self.config.get("fixture_path"), since)

    def _fetch_live(self, query: str, key: str, n: int,
                    since: Optional[datetime]) -> Iterator[RawRecord]:
        url = "https://api.twitterapi.io/twitter/tweet/advanced_search"
        try:
            r = requests.get(url, params={"query": query, "queryType": "Latest"},
                             headers={"X-API-Key": key}, timeout=15)
            r.raise_for_status()
            data = r.json()
        except Exception as e:                             # noqa: BLE001
            print(f"  [x_stub] live fetch failed: {e}; falling back to fixture")
            yield from self._fetch_fixture(None, since)
            return

        for t in (data.get("tweets") or [])[:n]:
            posted = self._parse_dt(t.get("createdAt"))
            if since and posted and posted <= since:
                continue
            yield RawRecord(
                external_id=f"x:{t['id']}",
                url=f"https://x.com/i/status/{t['id']}",
                text=t.get("text", ""),
                posted_at=posted or datetime.now(timezone.utc),
                author_handle=t.get("author", {}).get("userName"),
                language_hint=t.get("lang"),
                source_kind=self.source_kind,
                raw_blob=json.dumps(t)[:2000],
            )

    def _fetch_fixture(self, path: Optional[str],
                       since: Optional[datetime]) -> Iterator[RawRecord]:
        p = Path(path) if path else DEFAULT_FIXTURE
        if not p.exists():
            print(f"  [x_stub] fixture not found at {p}")
            return
        items = json.loads(p.read_text())
        for t in items:
            posted = self._parse_dt(t.get("created_at")) or datetime.now(timezone.utc)
            if since and posted <= since:
                continue
            yield RawRecord(
                external_id=f"x:{t['id']}",
                url=t.get("url") or f"https://x.com/i/status/{t['id']}",
                text=t.get("text", ""),
                posted_at=posted,
                author_handle=t.get("author"),
                language_hint=t.get("lang", "en"),
                source_kind=self.source_kind,
                raw_blob=json.dumps(t),
            )

    @staticmethod
    def _parse_dt(s) -> Optional[datetime]:
        if not s:
            return None
        try:
            d = dtparser.parse(s)
            return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d
        except Exception:                                  # noqa: BLE001
            return None


# ---------------------------------------------------------------------------
# Synthetic demo connector — generates reference-shaped pharmacovigilance
# chatter, suitable for hackathon screencasts and the Investigation Workspace.
#
# Each call to fetch() yields ~150 RawRecords whose mix of drug/event terms
# is calibrated so the disproportionality job flags the four "headline" pairs
# from the Google AI Studio reference design at PRR ≥ 2 with χ² above the
# Yates threshold.
# ---------------------------------------------------------------------------
class SyntheticDemoConnector(BaseConnector):
    """Drop-in replacement for any connector when you need predictable signals.

    Params:
      seed:        int     — RNG seed (default 7)
      n_records:   int     — total synthetic posts to emit (default 150)
      surface:     str     — "reddit" | "x" | "forum"  (changes source_kind/url)
      pairs:       list    — override drug→event headline pairs.
                              Default = the four ref-design pairs.
    """

    DEFAULT_PAIRS = [
        # (drug, event, n_with_pair, sentiment_bias, surface_template)
        ("Lisinopril",  "Angioedema",       18, "negative",
         "Started Lisinopril last week and now have severe angioedema swelling around my lips."),
        ("Metformin",   "Lactic Acidosis",  14, "negative",
         "Has anyone experienced lactic acidosis on Metformin? My labs show elevated lactate."),
        ("Dermacult",   "Pustular Rash",    12, "negative",
         "Used Dermacult cream for two days. Developed a painful pustular rash spreading on my arm."),
        ("Somnifert-X", "Severe Insomnia",  16, "adverse",
         "Started Somnifert-X for sleep, now experiencing severe insomnia and panic at night."),
    ]

    PAIR_VARIANTS = {
        ("Lisinopril", "Angioedema"): [
            "{drug} caused angioedema — lips swelled within hours, ER visit.",
            "Doctor switched me from {drug} after I developed angioedema.",
            "Anyone else got angioedema from {drug}? Tongue swelling scared me.",
            "Three days on {drug} → angioedema. Stopped immediately.",
            "My mother is on {drug} and had angioedema this week.",
            "Severe angioedema after my second {drug} dose. Going to urgent care.",
        ],
        ("Metformin", "Lactic Acidosis"): [
            "Lactic acidosis on {drug}? My readings are elevated, worried.",
            "Hospitalized with lactic acidosis while on {drug} for type 2.",
            "GP suspects {drug}-induced lactic acidosis given my kidney numbers.",
            "Lactic acidosis episode last night, currently on {drug}.",
            "ER visit — lactic acidosis flagged on {drug} therapy.",
        ],
        ("Dermacult", "Pustular Rash"): [
            "Developed a severe pustular rash after starting {drug} cream.",
            "{drug} caused me a painful pustular rash on both forearms.",
            "Started {drug} two days ago — now have a pustular rash spreading.",
            "Side effect of {drug}: pustular rash that won’t go away.",
            "Reaction: pustular rash after taking {drug} for the first time.",
        ],
        ("Somnifert-X", "Severe Insomnia"): [
            "{drug} gave me severe insomnia — opposite of what it should do.",
            "Severe insomnia and panic on {drug}, can’t sleep at all.",
            "Three nights of severe insomnia after starting {drug}.",
            "Doctor prescribed {drug} but I have severe insomnia now.",
            "Stopping {drug} — severe insomnia is intolerable.",
        ],        # ---- Historical: Zantac / ranitidine NDMA contamination ----
        ("Ranitidine", "NDMA contamination"): [
            "Tested my {drug} supply — found NDMA contamination levels way above FDA limit.",
            "Class action lawsuit over {drug} NDMA contamination. Anyone else filing?",
            "{drug} recalled due to NDMA contamination. Was taking it daily for years.",
            "Pharmacist pulled {drug} off shelves citing NDMA contamination risk.",
            "Study confirms NDMA contamination in {drug} increases with storage temperature.",
            "FDA warning: NDMA contamination in {drug} batches. Check your lot numbers.",
            "Switched from {drug} to famotidine after the NDMA contamination reports.",
        ],
        ("Ranitidine", "Cancer risk"): [
            "Worried about cancer risk from years of {drug} use due to NDMA.",
            "Oncologist says {drug} cancer risk from NDMA is real — urges patients to stop.",
            "Is there a proven cancer risk from taking {drug} long-term?",
            "Multiple lawsuits claim {drug} cancer risk was hidden by manufacturer.",
            "Research paper links long-term {drug} use to elevated cancer risk.",
        ],
        ("Ranitidine", "Liver damage"): [
            "Blood work showed liver damage — been on {drug} for acid reflux for 3 years.",
            "Doctor suspects {drug} caused liver damage based on my elevated enzymes.",
            "Anyone experience liver damage while taking {drug}?",
            "{drug} side effect: liver damage confirmed by biopsy.",
        ],    }

    DRUG_NOISE = [
        "Aspirin", "Ibuprofen", "Atorvastatin", "Amlodipine",
        "Sertraline", "Pantoprazole",
    ]
    EVENT_NOISE = [
        "headache", "nausea", "fatigue", "dizziness",
        "constipation", "dry mouth",
    ]
    NEUTRAL_TEMPLATES = [
        "Refilled my {drug} prescription today. No issues to report.",
        "Pharmacist confirmed {drug} is back in stock at the chain near me.",
        "Article on {drug} dosing for elderly patients was helpful.",
    ]
    POSITIVE_TEMPLATES = [
        "{drug} has been working well for me — fewer episodes overall.",
        "Switched to {drug} and quality of life improved significantly.",
    ]

    def __init__(self, config: dict):
        super().__init__(config)
        self.seed = int(config.get("seed", 7))
        self.n_records = int(config.get("n_records", 150))
        self.surface = config.get("surface", "reddit")
        self.pairs = config.get("pairs") or self.DEFAULT_PAIRS
        self.lookback_days = int(config.get("lookback_days", 30))

    def fetch(self, since: Optional[datetime] = None) -> Iterator[RawRecord]:
        import random
        rng = random.Random(self.seed)
        now = datetime.now(timezone.utc)
        lb = self.lookback_days
        emitted = 0

        # 1. Drug↔event "headline" co-occurrences — these drive PRR.
        for drug, event, n, _bias, default_tmpl in self.pairs:
            variants = self.PAIR_VARIANTS.get((drug, event), [default_tmpl])
            for k in range(n):
                day_offset = max(0, int(lb - (k * lb / max(n, 1)) ** 0.7))
                posted = now - timedelta(days=day_offset,
                                         hours=rng.randint(0, 23),
                                         minutes=rng.randint(0, 59))
                tmpl = rng.choice(variants + [default_tmpl])
                # Append a unique tag so each record's hash differs.
                text = (tmpl.format(drug=drug)
                        + f" #pv{self.surface[:3]}{emitted:04d}")
                yield self._mk(drug, event, text, posted, emitted)
                emitted += 1

        # 2. Drug-only mentions (PRR denominator) — neutral / positive.
        #    Keep these low relative to adverse pairs so PRR stays elevated.
        for drug, _, n_pair, _, _ in self.pairs:
            neutral_count = rng.randint(max(3, n_pair // 4), max(5, n_pair // 3))
            for j in range(neutral_count):
                day_offset = rng.randint(0, lb - 1)
                posted = now - timedelta(days=day_offset,
                                         hours=rng.randint(0, 23),
                                         minutes=rng.randint(0, 59))
                tmpl = rng.choice(self.NEUTRAL_TEMPLATES + self.POSITIVE_TEMPLATES)
                text = tmpl.format(drug=drug) + f" #n{emitted:04d}"
                yield self._mk(drug, None, text, posted, emitted)
                emitted += 1

        # 3. Event-only noise — use UNRELATED events so target-event PRR isn't diluted.
        for _ in range(rng.randint(6, 12)):
            day_offset = rng.randint(0, lb - 1)
            posted = now - timedelta(days=day_offset,
                                     hours=rng.randint(0, 23))
            noise_drug = rng.choice(self.DRUG_NOISE)
            noise_event = rng.choice(self.EVENT_NOISE)
            text = (f"Friend was on {noise_drug} when {noise_event} started. "
                    f"Side effect suspected. #e{emitted:04d}")
            yield self._mk(noise_drug, noise_event, text, posted, emitted)
            emitted += 1

        # 4. Background noise — neither drug nor event, pads the (-,-) cell.
        target = max(self.n_records, emitted)
        while emitted < target:
            day_offset = rng.randint(0, lb - 1)
            posted = now - timedelta(days=day_offset,
                                     hours=rng.randint(0, 23))
            d = rng.choice(self.DRUG_NOISE)
            text = (f"General health discussion about {d}, no issues. "
                    f"#bg{emitted:04d}")
            yield self._mk(d, None, text, posted, emitted)
            emitted += 1

    def _mk(self, drug: str, event: Optional[str], text: str,
            posted: datetime, idx: int) -> RawRecord:
        # Hash the full text (which is unique per emission) so dedup keeps it.
        external_id = (f"{self.surface}-demo-{self.seed}-{idx:05d}-"
                       + hashlib.md5(text.encode()).hexdigest()[:10])
        url = {
            "reddit": f"https://reddit.com/r/synthetic/{external_id}",
            "x":      f"https://x.com/synth/{external_id}",
            "forum":  f"https://forum.synth/{external_id}",
        }.get(self.surface, f"https://synth/{external_id}")
        return RawRecord(
            external_id=external_id,
            text=text,
            url=url,
            posted_at=posted,
            author_handle=f"user_{idx % 200}",
            language_hint="en",
            source_kind=self.surface,
            raw_blob=text,
        )


# ---------------------------------------------------------------------------
# Auto-register into the spike1 REGISTRY
# ---------------------------------------------------------------------------
def _register():
    try:
        import spike1_connectors as s1  # noqa: WPS433
    except ImportError:
        return
    s1.REGISTRY.setdefault("html_stealth", HtmlStealthConnector)
    s1.REGISTRY.setdefault("html_auto", HtmlAutoConnector)
    s1.REGISTRY.setdefault("html", HtmlAutoConnector)
    s1.REGISTRY.setdefault("html_listing", HtmlListingConnector)
    s1.REGISTRY.setdefault("openfda", OpenFdaConnector)
    # Override the spike's stub FAERSConnector (which only had 4 synthetic
    # rows) with the real openFDA-backed connector. Source params can still
    # use connector: faers in YAML — they will now pull real FAERS data via
    # api.fda.gov when params include a `search` query.
    s1.REGISTRY["faers"] = OpenFdaConnector
    s1.REGISTRY.setdefault("whatsapp", WhatsAppConnector)
    # Override the spike's PRAW-only Reddit connector with our richer one.
    s1.REGISTRY["reddit"] = RedditPlusConnector
    s1.REGISTRY.setdefault("reddit_plus", RedditPlusConnector)
    s1.REGISTRY.setdefault("x", XStubConnector)
    s1.REGISTRY.setdefault("x_stub", XStubConnector)
    # Synthetic demo connector intentionally NOT registered: real-data only mode.
    # To re-enable for offline demos, uncomment:
    # s1.REGISTRY.setdefault("synthetic_demo", SyntheticDemoConnector)


_register()
