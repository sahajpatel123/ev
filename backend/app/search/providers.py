"""Web search providers behind a swappable, config-level interface (plan 11.3).

Default is memory-only (``EV_SEARCH_PROVIDER=none``): no key, no network, no
results. ``mock`` is deterministic for tests; ``brave`` uses the user's own
Brave Search API key. Every result carries a citation (title, url, snippet).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.config import settings

_RETRY_DELAY_SECONDS = 0.5
_CACHE_TTL_SECONDS = 900.0
_CACHE_MAX_ENTRIES = 200
# query+limit -> (expires_at, results). Small in-process TTL so repeated
# MiMo-kernel turns and HUD refreshes don't re-hit the network.
_SEARCH_CACHE: dict[str, tuple[float, list]] = {}


def _cache_key(name: str, query: str, limit: int) -> str:
    return f"{name}:{limit}:{query.strip().lower()}"


def cached_results(name: str, query: str, limit: int) -> list | None:
    entry = _SEARCH_CACHE.get(_cache_key(name, query, limit))
    if entry is None:
        return None
    expires_at, results = entry
    if time.monotonic() > expires_at:
        _SEARCH_CACHE.pop(_cache_key(name, query, limit), None)
        return None
    return [SearchResult(title=r.title, url=r.url, snippet=r.snippet) for r in results]


def store_results(name: str, query: str, limit: int, results: list) -> None:
    if len(_SEARCH_CACHE) >= _CACHE_MAX_ENTRIES:
        oldest = min(_SEARCH_CACHE, key=lambda k: _SEARCH_CACHE[k][0])
        _SEARCH_CACHE.pop(oldest, None)
    _SEARCH_CACHE[_cache_key(name, query, limit)] = (
        time.monotonic() + _CACHE_TTL_SECONDS,
        list(results),
    )


def clear_search_cache() -> None:
    _SEARCH_CACHE.clear()


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        return 500 <= exc.response.status_code < 600
    return False


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str


class SearchProvider(Protocol):
    name: str

    async def search(self, query: str, *, limit: int = 5) -> list[SearchResult]: ...


class MockSearchProvider:
    """Deterministic provider for offline dev and tests."""

    name = "mock"

    async def search(self, query: str, *, limit: int = 5) -> list[SearchResult]:
        limit = max(1, min(int(limit), 10))
        return [
            SearchResult(
                title=f"Result {i + 1} for '{query[:40]}'",
                url=f"https://example.com/{i + 1}",
                snippet=f"Mock citation {i + 1} about {query[:40]}.",
            )
            for i in range(limit)
        ]


class BraveSearchProvider:
    """Brave Search Web Search API (user-supplied key)."""

    name = "brave"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.search.brave.com/res/v1/web/search",
        timeout_seconds: float = 10.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds

    async def search(self, query: str, *, limit: int = 5) -> list[SearchResult]:
        limit = min(max(int(limit), 1), 10)
        hit = cached_results(self.name, query, limit)
        if hit is not None:
            return hit
        resp = None  # assigned inside retry loop
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    resp = await client.get(
                        self.base_url,
                        params={"q": query, "count": min(max(int(limit), 1), 10)},
                        headers={"X-Subscription-Token": self.api_key, "Accept": "application/json"},
                    )
                    resp.raise_for_status()
                break
            except Exception as exc:
                if attempt == 0 and _is_transient(exc):
                    await asyncio.sleep(_RETRY_DELAY_SECONDS)
                    continue
                raise
        assert resp is not None
        data = resp.json()
        results = []
        for item in (data.get("web") or {}).get("results") or []:
            results.append(
                SearchResult(
                    title=item.get("title") or "",
                    url=item.get("url") or "",
                    snippet=item.get("description") or "",
                )
            )
        results = results[:limit]
        store_results(self.name, query, limit, results)
        return results


def get_search_provider() -> SearchProvider | None:
    """Config-level selection; None means memory-only research."""

    name = settings.search_provider
    if name == "mock":
        return MockSearchProvider()
    if name in {"live", "open", "openmeteo"}:
        from app.search.live import LiveSearchProvider

        return LiveSearchProvider()
    if name == "brave":
        if not settings.brave_search_api_key:
            return None
        return BraveSearchProvider(
            api_key=settings.brave_search_api_key,
            base_url=settings.brave_search_base_url,
            timeout_seconds=settings.search_timeout_seconds,
        )
    return None
