"""
SrotaAI error system.

Every user-triggered operation is funnelled through `safe()` so that:
  - Custom exceptions carry a user-facing message + hint.
  - Unexpected exceptions are caught, logged with full traceback, and
    presented to the user as a clean banner with a "show technical details"
    affordance — never a raw traceback.
  - A structured event is appended to the in-memory error log shown in the
    Debug expander.

Used by the dashboard, validators, and any background helper.
"""
from __future__ import annotations

import logging
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional, TypeVar

log = logging.getLogger("srotaai")


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------
class SrotaError(Exception):
    """Base for every domain error. Carries a clean user message + hint."""
    user_message: str = "Something went wrong."
    hint: Optional[str] = None
    code: str = "srota_error"

    def __init__(self, technical: str = "", *,
                 user_message: Optional[str] = None,
                 hint: Optional[str] = None,
                 cause: Optional[BaseException] = None,
                 ctx: Optional[dict] = None):
        super().__init__(technical or self.user_message)
        if user_message:
            self.user_message = user_message
        if hint is not None:
            self.hint = hint
        self.cause = cause
        self.ctx = ctx or {}


class ValidationError(SrotaError):
    user_message = "Please fix the highlighted fields."
    hint = "Required information is missing or formatted incorrectly."
    code = "validation"


class DatabaseError(SrotaError):
    user_message = "We couldn't save to the database."
    hint = ("This is usually transient — try again in a moment. "
            "If it persists, check disk space and that no other "
            "process is holding the DB locked.")
    code = "db"


class DuplicateError(SrotaError):
    user_message = "That project ID already exists."
    hint = "Pick a different ID, or open the existing project from the sidebar."
    code = "duplicate"


class ConnectorError(SrotaError):
    user_message = "A source couldn't be configured."
    hint = "Check the connector type and parameters."
    code = "connector"


class FetchError(SrotaError):
    user_message = "Fetching from one or more sources failed."
    hint = ("The source might be temporarily unreachable, behind auth, "
            "or rate-limited. You can retry just the failed sources.")
    code = "fetch"


class AnalyticsError(SrotaError):
    user_message = "Analytics computation failed."
    hint = "Make sure data has been fetched first, then try again."
    code = "analytics"


class StaleStateError(SrotaError):
    user_message = "The page state is out of date."
    hint = "Reloading from the database — please retry your last action."
    code = "stale"


# ---------------------------------------------------------------------------
# Retry helpers (DB-locked, transient network)
# ---------------------------------------------------------------------------
T = TypeVar("T")


def with_retry(
    fn: Callable[[], T],
    *,
    retries: int = 4,
    base_delay: float = 0.05,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    op: str = "op",
    ctx: Optional[dict] = None,
) -> T:
    """Exponential-backoff retry for transient failures.

    Used by Store for `database is locked` and by connectors for HTTP timeouts.
    """
    last: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            return fn()
        except retry_on as e:                                   # noqa: BLE001
            last = e
            if attempt == retries:
                break
            delay = base_delay * (2 ** attempt)
            log.warning("retry op=%s attempt=%d/%d err=%s ctx=%s "
                        "next_delay=%.3fs",
                        op, attempt + 1, retries, type(e).__name__,
                        ctx or {}, delay)
            time.sleep(delay)
    assert last is not None
    raise last


# ---------------------------------------------------------------------------
# In-memory event log (debug panel)
# ---------------------------------------------------------------------------
@dataclass
class ErrorEvent:
    ts: str
    op: str
    error_type: str
    user_message: str
    technical: str
    ctx: dict = field(default_factory=dict)
    traceback: str = ""

    def to_dict(self) -> dict:
        return {
            "ts": self.ts, "op": self.op, "error_type": self.error_type,
            "user_message": self.user_message, "technical": self.technical,
            "ctx": self.ctx, "traceback": self.traceback,
        }


_EVENTS: list[ErrorEvent] = []
_MAX_EVENTS = 50


def record_event(op: str, exc: BaseException,
                 ctx: Optional[dict] = None) -> ErrorEvent:
    if isinstance(exc, SrotaError):
        user_msg = exc.user_message
        technical = str(exc) or repr(exc)
    else:
        user_msg = "Unexpected error."
        technical = f"{type(exc).__name__}: {exc}"
    ev = ErrorEvent(
        ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        op=op, error_type=type(exc).__name__,
        user_message=user_msg, technical=technical,
        ctx=ctx or {},
        traceback="".join(traceback.format_exception(
            type(exc), exc, exc.__traceback__))[:5000],
    )
    _EVENTS.append(ev)
    if len(_EVENTS) > _MAX_EVENTS:
        del _EVENTS[: len(_EVENTS) - _MAX_EVENTS]
    log.error("op=%s type=%s msg=%s ctx=%s",
              op, ev.error_type, technical, ctx or {})
    return ev


def recent_events(limit: int = 20) -> list[dict]:
    return [e.to_dict() for e in _EVENTS[-limit:][::-1]]


def clear_events() -> None:
    _EVENTS.clear()


# ---------------------------------------------------------------------------
# safe() — the funnel
# ---------------------------------------------------------------------------
@dataclass
class SafeResult:
    ok: bool
    value: Any = None
    error: Optional[ErrorEvent] = None
    hint: Optional[str] = None

    @property
    def user_message(self) -> Optional[str]:
        return self.error.user_message if self.error else None


def safe(op: str, fn: Callable[[], T],
         *, ctx: Optional[dict] = None) -> SafeResult:
    """Call `fn()` and never let an exception escape.

    Returns SafeResult — caller checks `.ok` and renders a banner.
    """
    try:
        v = fn()
        return SafeResult(ok=True, value=v)
    except BaseException as e:                                 # noqa: BLE001
        ev = record_event(op, e, ctx)
        hint = e.hint if isinstance(e, SrotaError) else None
        return SafeResult(ok=False, value=None, error=ev, hint=hint)
