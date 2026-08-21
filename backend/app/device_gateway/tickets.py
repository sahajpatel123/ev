"""One-time short-lived WebSocket tickets. Never put long-lived device tokens in URLs."""

from __future__ import annotations

import secrets
import time
from typing import Any
from uuid import UUID

_TTL_S = 45.0
_TICKETS: dict[str, dict[str, Any]] = {}


def mint(*, device_id: UUID, session_id: str, instance_id: str = "") -> str:
    _gc()
    ticket = secrets.token_urlsafe(24)
    _TICKETS[ticket] = {
        "device_id": str(device_id),
        "session_id": str(session_id),
        "instance_id": instance_id[:64],
        "exp": time.time() + _TTL_S,
        "used": False,
    }
    return ticket


def consume(ticket: str, *, session_id: str | None = None) -> dict[str, Any] | None:
    _gc()
    row = _TICKETS.get(ticket or "")
    if row is None or row.get("used") or float(row.get("exp") or 0) < time.time():
        return None
    if session_id and str(row.get("session_id")) != str(session_id):
        return None
    row["used"] = True
    _TICKETS.pop(ticket, None)
    return dict(row)


def _gc() -> None:
    now = time.time()
    for key, row in list(_TICKETS.items()):
        if row.get("used") or float(row.get("exp") or 0) < now:
            _TICKETS.pop(key, None)
