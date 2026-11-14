"""
HTTP fetch with a retry ladder for evading anti-bot defenses.

Rungs (try in order, stop at first 200):
    1. plain `requests`           — cheapest
    2. `curl_cffi` (impersonate)   — TLS fingerprint of real Chrome
    3. `playwright` headful-ish   — full JS render
    4. proxy + playwright          — last resort

Each rung is optional. We import lazily and skip rungs whose deps are missing,
so the module works with bare requests on a quota-constrained host. The same
shape can absorb a real proxy pool in production without changing callers.
"""
from __future__ import annotations

import ipaddress
import os
import random
import socket
import time
from urllib.parse import urljoin, urlparse
from dataclasses import dataclass, field
from typing import Optional

import requests

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_4) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0 Safari/537.36",
]


@dataclass
class FetchResult:
    url: str
    status: int = 0
    text: str = ""
    rung: str = ""                     # which rung succeeded
    attempts: list[dict] = field(default_factory=list)
    elapsed_s: float = 0.0
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and bool(self.text)


# ---------------------------------------------------------------------------
# SSRF guard — project sources take user-supplied URLs that this server fetches,
# so refuse anything that resolves to a private / loopback / link-local /
# cloud-metadata address. Set SROTAAI_ALLOW_PRIVATE_URLS=1 to disable (local dev).
# ---------------------------------------------------------------------------
class UnsafeURLError(ValueError):
    """Raised when a URL targets a non-public network location."""


def assert_public_url(url: str) -> None:
    if os.environ.get("SROTAAI_ALLOW_PRIVATE_URLS") == "1":
        return
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UnsafeURLError(f"only http(s) URLs with a host are allowed: {url!r}")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 443,
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise UnsafeURLError(f"cannot resolve host {parts.hostname!r}: {e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise UnsafeURLError(
                f"{parts.hostname!r} resolves to non-public address {ip}")


# ---------------------------------------------------------------------------
# Rungs
# ---------------------------------------------------------------------------
def _rung_requests(url: str, timeout: float) -> tuple[int, str]:
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
    }
    # Follow redirects by hand so every hop is re-validated by the SSRF guard.
    for _ in range(5):
        r = requests.get(url, headers=headers, timeout=timeout,
                         allow_redirects=False)
        if r.is_redirect and r.headers.get("Location"):
            url = urljoin(url, r.headers["Location"])
            assert_public_url(url)
            continue
        return r.status_code, r.text
    return 310, ""   # too many redirects


def _rung_curl_cffi(url: str, timeout: float) -> tuple[int, str]:
    from curl_cffi import requests as ccr  # type: ignore
    r = ccr.get(url, impersonate="chrome120", timeout=timeout)
    return r.status_code, r.text


def _rung_playwright(url: str, timeout: float) -> tuple[int, str]:
    # Optional — needs `pip install playwright && playwright install chromium`.
    from playwright.sync_api import sync_playwright  # type: ignore
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=random.choice(USER_AGENTS))
        page = ctx.new_page()
        resp = page.goto(url, timeout=int(timeout * 1000), wait_until="domcontentloaded")
        text = page.content()
        status = resp.status if resp else 0
        browser.close()
        return status, text


RUNGS = [
    ("requests",   _rung_requests),
    ("curl_cffi",  _rung_curl_cffi),
    ("playwright", _rung_playwright),
]


def fetch(url: str, timeout: float = 20.0,
          rungs: Optional[list[str]] = None,
          backoff_s: float = 0.4) -> FetchResult:
    """Walk the retry ladder. Returns the first successful response, or the
    last failure with attempts recorded for debugging."""
    chosen = [(n, fn) for (n, fn) in RUNGS if (rungs is None or n in rungs)]
    res = FetchResult(url=url)
    t0 = time.time()
    try:
        assert_public_url(url)
    except UnsafeURLError as e:
        res.error = f"blocked: {e}"
        return res
    for name, fn in chosen:
        a: dict = {"rung": name}
        t = time.time()
        try:
            status, text = fn(url, timeout)
            a.update(status=status, elapsed_s=round(time.time() - t, 3),
                     text_len=len(text or ""))
            res.attempts.append(a)
            if 200 <= status < 300 and text:
                res.status = status
                res.text = text
                res.rung = name
                res.elapsed_s = round(time.time() - t0, 3)
                return res
            # 403/429/blocked — climb the ladder
            time.sleep(backoff_s)
        except ImportError as e:
            a.update(skipped=True, reason=str(e))
            res.attempts.append(a)
            continue
        except Exception as e:
            a.update(error=f"{type(e).__name__}: {e}",
                     elapsed_s=round(time.time() - t, 3))
            res.attempts.append(a)
            time.sleep(backoff_s)
            continue

    res.elapsed_s = round(time.time() - t0, 3)
    res.error = "all rungs exhausted"
    return res


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys, json
    target = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
    r = fetch(target)
    print(json.dumps({
        "ok": r.ok, "status": r.status, "rung": r.rung,
        "elapsed_s": r.elapsed_s, "attempts": r.attempts,
        "text_preview": (r.text or "")[:120].replace("\n", " "),
    }, indent=2))
