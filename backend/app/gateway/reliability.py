"""API-only reliability: timeouts, bounded retries, circuit breaker.

CORTEX follow-up order 5: with DeepSeek as the only reasoning provider, an
outage must degrade cleanly instead of hanging every request. This module owns
the transport policy:

* configured connect/read/write/pool timeouts,
* bounded retries with jittered exponential backoff for transient failures,
* a per-provider circuit breaker (closed → open → half-open) with fast-fail
  ``CircuitOpenError`` so the gateway can surface degradation in the envelope.

Streaming never retries after the first byte has been delivered; a mid-stream
upstream failure is surfaced as a typed error instead of a truncated success.
"""

from __future__ import annotations

import asyncio
import random
import threading
import time
from dataclasses import dataclass

import httpx

from app.config import settings


class CircuitOpenError(RuntimeError):
    """The provider circuit breaker is open; requests fail fast by policy."""

    def __init__(self, provider: str, retry_after_seconds: float) -> None:
        self.provider = provider
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            f"{provider} circuit breaker is open; retry after "
            f"{retry_after_seconds:.0f}s"
        )


class ProviderStreamError(RuntimeError):
    """A typed mid-stream upstream failure (after partial output)."""


def http_timeout() -> httpx.Timeout:
    """Timeout policy for every provider HTTP call (env-configurable)."""

    return httpx.Timeout(
        connect=settings.model_connect_timeout_seconds,
        read=settings.model_read_timeout_seconds,
        write=settings.model_write_timeout_seconds,
        pool=settings.model_pool_timeout_seconds,
    )


def retry_delay_seconds(attempt: int) -> float:
    """Jittered exponential backoff for one retry attempt (0-based)."""

    base = settings.model_retry_base_seconds * (2**attempt)
    base = min(base, settings.model_retry_max_seconds)
    jitter = random.uniform(0.7, 1.3)
    return round(base * jitter, 3)


def max_attempts() -> int:
    return 1 + max(0, settings.model_max_retries)


def is_transient(exc: Exception, status_code: int | None = None) -> bool:
    """True for failures that are safe to retry."""

    if isinstance(exc, (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout)):
        return True
    if isinstance(exc, httpx.RemoteProtocolError):
        return True
    if isinstance(exc, httpx.TransportError):
        return True
    return status_code is not None and status_code in (429, 500, 502, 503, 504)


@dataclass
class CircuitState:
    state: str  # closed | open | half_open
    failure_count: int = 0
    success_count: int = 0
    opened_at: float | None = None


class CircuitBreaker:
    """Per-provider failure breaker with a half-open probe window."""

    def __init__(
        self,
        *,
        failure_threshold: int | None = None,
        cooldown_seconds: float | None = None,
        half_open_success_threshold: int | None = None,
    ) -> None:
        self.failure_threshold = (
            failure_threshold or settings.circuit_failure_threshold
        )
        self.cooldown_seconds = cooldown_seconds or settings.circuit_cooldown_seconds
        self.half_open_success_threshold = (
            half_open_success_threshold
            or settings.circuit_half_open_success_threshold
        )
        self._lock = threading.Lock()
        self._state = CircuitState(state="closed")

    def state(self) -> dict:
        with self._lock:
            return {
                "state": self._state.state,
                "failure_count": self._state.failure_count,
                "success_count": self._state.success_count,
                "opened_at": self._state.opened_at,
            }

    def allow_request(self) -> bool:
        """True when a request may proceed; open circuits fast-fail."""

        with self._lock:
            if self._state.state == "closed":
                return True
            if self._state.state == "half_open":
                return True
            opened_at = self._state.opened_at or 0.0
            if time.monotonic() - opened_at >= self.cooldown_seconds:
                self._state.state = "half_open"
                self._state.success_count = 0
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self._state.failure_count = 0
            if self._state.state == "half_open":
                self._state.success_count += 1
                if self._state.success_count >= self.half_open_success_threshold:
                    self._state.state = "closed"
                    self._state.opened_at = None
                    self._state.success_count = 0

    def record_failure(self) -> None:
        with self._lock:
            if self._state.state == "half_open":
                self._state.state = "open"
                self._state.opened_at = time.monotonic()
                self._state.success_count = 0
                return
            self._state.failure_count += 1
            if self._state.failure_count >= self.failure_threshold:
                self._state.state = "open"
                self._state.opened_at = time.monotonic()

    def retry_after_seconds(self) -> float:
        with self._lock:
            if self._state.opened_at is None:
                return 0.0
            remaining = self.cooldown_seconds - (time.monotonic() - self._state.opened_at)
            return round(max(0.0, remaining), 1)


class CircuitBreakerRegistry:
    """One breaker per provider name, shared process-wide."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._breakers: dict[str, CircuitBreaker] = {}

    def get(self, provider: str) -> CircuitBreaker:
        with self._lock:
            breaker = self._breakers.get(provider)
            if breaker is None:
                breaker = CircuitBreaker()
                self._breakers[provider] = breaker
            return breaker

    def reset(self, provider: str | None = None) -> None:
        with self._lock:
            if provider is None:
                self._breakers.clear()
            else:
                self._breakers.pop(provider, None)


CIRCUIT_BREAKERS = CircuitBreakerRegistry()


async def wait_for_retry(attempt: int) -> None:
    await asyncio.sleep(retry_delay_seconds(attempt))
