"""Structured memory diagnostics. Never log secrets or raw media."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("ev.memory")


def log_memory(event: str, *, extra: dict[str, Any] | None = None) -> None:
    payload = dict(extra or {})
    payload.pop("text", None)
    payload.pop("jpeg", None)
    payload.pop("image", None)
    payload.pop("audio", None)
    parts = " ".join(f"{key}={value}" for key, value in payload.items() if value is not None)
    if parts:
        logger.info("memory_trace event=%s %s", event, parts)
    else:
        logger.info("memory_trace event=%s", event)
