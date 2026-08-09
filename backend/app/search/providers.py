"""Web search providers behind a swappable, config-level interface (plan 11.3).

Default is memory-only (``EV_SEARCH_PROVIDER=none``): no key, no network, no
results. ``mock`` is deterministic for tests; ``brave`` uses the user's own
Brave Search API key. Every result carries a citation (title, url, snippet).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx

from app.config import settings


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
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            resp = await client.get(
                self.base_url,
                params={"q": query, "count": min(max(int(limit), 1), 10)},
                headers={"X-Subscription-Token": self.api_key, "Accept": "application/json"},
            )
            resp.raise_for_status()
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
        return results[: min(max(int(limit), 1), 10)]


def get_search_provider() -> SearchProvider | None:
    """Config-level selection; None means memory-only research."""

    name = settings.search_provider
    if name == "mock":
        return MockSearchProvider()
    if name == "brave":
        if not settings.brave_search_api_key:
            return None
        return BraveSearchProvider(
            api_key=settings.brave_search_api_key,
            base_url=settings.brave_search_base_url,
            timeout_seconds=settings.search_timeout_seconds,
        )
    return None
