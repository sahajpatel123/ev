"""Tests for permissioned web/search with citations (plan 11.3)."""

from __future__ import annotations

from httpx import AsyncClient

from app.config import settings
from app.search import providers
from app.search.providers import (
    BraveSearchProvider,
    MockSearchProvider,
    get_search_provider,
)


async def test_mock_provider_returns_citations() -> None:
    results = await MockSearchProvider().search("EV companion", limit=3)
    assert len(results) == 3
    for result in results:
        assert result.title
        assert result.url.startswith("https://")
        assert result.snippet


async def test_brave_provider_maps_results(monkeypatch) -> None:
    captured: list = []

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "web": {
                    "results": [
                        {
                            "title": "EV docs",
                            "url": "https://example.com/ev",
                            "description": "How EV works.",
                        }
                    ]
                }
            }

    class _FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def get(self, url: str, **kwargs) -> _FakeResponse:
            captured.append((url, kwargs))
            return _FakeResponse()

    monkeypatch.setattr(providers.httpx, "AsyncClient", _FakeClient)
    provider = BraveSearchProvider(api_key="test-key")

    results = await provider.search("EV", limit=1)

    assert len(results) == 1
    assert results[0].title == "EV docs"
    assert results[0].url == "https://example.com/ev"
    assert results[0].snippet == "How EV works."
    url, kwargs = captured[0]
    assert "count" in kwargs["params"]
    assert kwargs["headers"]["X-Subscription-Token"] == "test-key"


async def test_provider_factory_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(settings, "search_provider", "none")
    assert get_search_provider() is None

    monkeypatch.setattr(settings, "search_provider", "brave")
    monkeypatch.setattr(settings, "brave_search_api_key", None)
    assert get_search_provider() is None  # no key -> no network, memory-only

    monkeypatch.setattr(settings, "search_provider", "mock")
    assert isinstance(get_search_provider(), MockSearchProvider)


async def test_unknown_search_provider_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(settings, "search_provider", "does-not-exist")
    assert get_search_provider() is None  # never fabricates a provider or results


async def test_search_web_tool_dispatch_with_citations(
    client: AsyncClient, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "search_provider", "mock")
    resp = await client.post(
        "/v1/gateway/tools",
        json={"name": "search_web", "arguments": {"query": "EV companion", "limit": 2}},
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["ok"] is True
    results = payload["result"]["results"]
    assert len(results) == 2
    for result in results:
        assert result["title"]
        assert result["url"].startswith("https://")
        assert result["snippet"]


async def test_search_web_disabled_fails_closed(
    client: AsyncClient, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "search_provider", "none")
    resp = await client.post(
        "/v1/gateway/tools",
        json={"name": "search_web", "arguments": {"query": "anything"}},
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    # Capability-theater contract: honest degraded result with an exact next_step.
    assert payload["ok"] is True
    assert payload["result"]["degraded"] is True
    assert "EV_SEARCH_PROVIDER" in payload["result"]["next_step"]
    assert payload["result"]["count"] == 0
