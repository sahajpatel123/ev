"""Webhook ingress: HMAC-SHA256 verification, replay protection, rate limits.

External systems push signed payloads here; verified payloads are translated
by the integration's adapter and ingested into its bound live channel through
the same immutable, idempotent live-event pipeline used by every other source.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from collections import defaultdict, deque

from app.config import settings

SIGNATURE_PREFIX = "sha256="


class SignatureError(ValueError):
    """Malformed, expired, or mismatched webhook signature."""


class RateLimitError(Exception):
    """The integration exceeded its webhook rate limit."""


def _header_value(headers: dict | None, name: str) -> str | None:
    if not headers:
        return None
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def verify_webhook_signature(
    *,
    secret: str,
    body: bytes,
    headers: dict | None,
    now: float | None = None,
) -> None:
    """Verify ``X-EV-Signature`` over ``X-EV-Timestamp.body``.

    Raises :class:`SignatureError` on any malformed, stale, or mismatched
    signature. The timestamp check bounds replay windows.
    """
    signature = _header_value(headers, "X-EV-Signature") or ""
    timestamp = _header_value(headers, "X-EV-Timestamp") or ""
    if not signature.startswith(SIGNATURE_PREFIX):
        raise SignatureError("missing or malformed X-EV-Signature")
    try:
        timestamp_float = float(timestamp)
    except (TypeError, ValueError):
        raise SignatureError("missing or malformed X-EV-Timestamp") from None
    now = time.time() if now is None else now
    if abs(now - timestamp_float) > settings.webhook_max_skew_seconds:
        raise SignatureError("webhook timestamp is outside the replay window")
    provided = signature[len(SIGNATURE_PREFIX) :]
    expected = hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.".encode("ascii") + body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(provided.lower(), expected):
        raise SignatureError("invalid webhook signature")


class SlidingWindowRateLimiter:
    """In-memory sliding-window limiter (Redis-backed in multi-process prod)."""

    def __init__(self, limit: int, window_seconds: float) -> None:
        self._limit = limit
        self._window = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            bucket = self._events[key]
            cutoff = now - self._window
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self._limit:
                return False
            bucket.append(now)
            return True


webhook_rate_limiter = SlidingWindowRateLimiter(
    settings.webhook_rate_limit,
    settings.webhook_window_seconds,
)
