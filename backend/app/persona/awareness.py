"""Situational awareness: the NOW line (spec §5).

Built only from signals the caller already holds. Missing signals are
omitted, never guessed. Max ~40 tokens (~160 chars under the repo
estimator); over-long lines shed trailing segments, never invent shorter
facts.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

_NOW_MAX_CHARS = 160
_SEP = " · "


def signals_from_context(ctx: Any) -> dict[str, Any]:
    """Extract NOW-line signals from a PersonaContext (duck-typed)."""
    work = getattr(ctx, "work_state", None)
    receipts = list(getattr(ctx, "receipts", None) or [])
    last_error: str | None = None
    last_win: str | None = None
    for receipt in reversed(receipts):
        ok, name, error = _receipt_parts(receipt)
        if ok is False and last_error is None:
            last_error = f"{name} failed" + (f": {error}" if error else "")
        elif ok is True and last_win is None:
            last_win = f"{name} ok"
        if last_error is not None and last_win is not None:
            break
    return {
        "local_time": getattr(ctx, "local_time", None),
        "session_started": getattr(ctx, "session_started", None),
        "task": getattr(work, "task", None),
        "last_result": getattr(work, "last_result", None),
        "last_error": last_error,
        "last_win": last_win,
        "first_commitment_tomorrow": getattr(ctx, "first_commitment_tomorrow", None),
    }


def _receipt_parts(receipt: Any) -> tuple[bool | None, str, str | None]:
    if isinstance(receipt, Mapping):
        ok = receipt.get("ok")
        name = str(receipt.get("name") or receipt.get("_tool") or "tool")
        error = receipt.get("error")
        return (None if ok is None else bool(ok), name, str(error) if error else None)
    ok = getattr(receipt, "ok", None)
    name = str(getattr(receipt, "name", None) or "tool")
    error = getattr(receipt, "error", None) or getattr(receipt, "result", None)
    if isinstance(error, dict):
        error = error.get("error")
    return (None if ok is None else bool(ok), name, str(error) if error else None)


def _fmt_time(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.strftime("%a %H:%M")
    if isinstance(value, str) and value.strip():
        return value.strip()[:5]
    return None


def _fmt_duration(start: Any, now: Any) -> str | None:
    if not isinstance(start, datetime) or not isinstance(now, datetime):
        return None
    if (start.tzinfo is None) != (now.tzinfo is None):
        start = start.replace(tzinfo=None)
        now = now.replace(tzinfo=None)
    seconds = max(0, int((now - start).total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def _clean(value: Any, limit: int = 48) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def build_now_line(signals: Mapping[str, Any], *, detail: str = "full") -> str:
    """Build the NOW line. Empty string when no signal is present."""
    if detail not in ("full", "short"):
        detail = "full"
    segments: list[str] = []
    clock = _fmt_time(signals.get("local_time"))
    if clock:
        segments.append(clock)
    duration = _fmt_duration(signals.get("session_started"), signals.get("local_time"))
    if duration:
        segments.append(f"session {duration}")
    task = _clean(signals.get("task"))
    if task:
        segments.append(f"working: {task}")
    last_result = _clean(signals.get("last_result"))
    if last_result:
        segments.append(f"last: {last_result}")
    last_error = _clean(signals.get("last_error"))
    if last_error:
        segments.append(f"error: {last_error}")
    else:
        last_win = _clean(signals.get("last_win"))
        if last_win:
            segments.append(f"win: {last_win}")
    offer = _clean(signals.get("recent_offer"))
    if offer:
        segments.append(f"offered: {offer}")
    decline = _clean(signals.get("recent_decline"))
    if decline:
        segments.append(f"declined: {decline}")
    first = _clean(signals.get("first_commitment_tomorrow"), limit=32)
    if first:
        segments.append(f"tomorrow first: {first}")
    if not segments:
        return ""
    if detail == "short":
        segments = segments[:2]
    line = "NOW: " + _SEP.join(segments)
    while len(line) > _NOW_MAX_CHARS and len(segments) > 1:
        segments = segments[:-1]
        line = "NOW: " + _SEP.join(segments)
    return line
