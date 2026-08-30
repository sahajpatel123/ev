"""Structured cross-platform telemetry. No secrets, no memory text, no frames."""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any

_LOG = logging.getLogger("ev.device_gateway")
_EVENTS: deque[dict[str, Any]] = deque(maxlen=200)


def emit(kind: str, **fields: Any) -> None:
    row = {"at": time.time(), "event": kind}
    for key, value in fields.items():
        if value is None:
            continue
        if key in {"token", "authorization", "jpeg_b64", "audio", "secret", "pairing_token", "device_token"}:
            continue
        row[key] = value
    _EVENTS.append(row)
    _LOG.info("telemetry %s", kind, extra={"ev_event": kind, "ev_fields": {k: v for k, v in row.items() if k != "at"}})


def recent(*, kind: str | None = None, limit: int = 40) -> list[dict[str, Any]]:
    rows = list(_EVENTS)
    if kind:
        rows = [row for row in rows if row.get("event") == kind]
    return rows[-limit:]
