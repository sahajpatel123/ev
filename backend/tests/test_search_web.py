"""Tests for permissioned web search (plan 11.3): provider seam + tool dispatch."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.ev.tools import dispatch, get_spec, list_tools
from app.search.providers import (
    BraveSearchProvider,
    MockSearchProvider,
    get_search_provider,
)


def test_search_provider_selection_is_config_level(monkeypatch) -> None:
    monkeypatch.setattr(settings, "search_provider", "mock")
    provider = get_search_provider()
    assert isinstance(provider, MockSearchProvider)

    monkeypatch.setattr(settings, "search_provider", "brave")
    monkeypatch.setattr(settings, "brave_search_api_key", None)
    assert get_search_provider() is None

    monkeypatch.setattr(settings, "brave_search_api_key", "key-123")
    provider = get_search_provider()
    assert isinstance(provider, BraveSearchProvider)

    monkeypatch.setattr(settings, "search_provider", "none")
    assert get_search_provider() is None


async def test_mock_search_returns_citations() -> None:
    provider = MockSearchProvider()
    results = await provider.search("EV personal AI companion", limit=3)
    assert len(results) == 3
    assert all(r.url.startswith("https://") for r in results)
    assert all(r.title and r.snippet for r in results)


async def test_search_web_tool_dispatch_with_mock(
    db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "search_provider", "mock")
    response = await dispatch(
        db_session,
        "search_web",
        {"query": "brave search api"},
    )
    assert response.ok is True, response.error
    assert response.result is not None
    assert response.result["count"] >= 1
    assert response.result["results"][0]["url"].startswith("https://")


async def test_search_web_tool_disabled_without_provider(
    db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "search_provider", "none")
    response = await dispatch(
        db_session,
        "search_web",
        {"query": "anything"},
    )
    assert response.ok is False
    assert "disabled" in (response.error or "")


async def test_search_web_tool_rejects_missing_query(
    db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "search_provider", "mock")
    response = await dispatch(db_session, "search_web", {})
    assert response.ok is False
    assert "missing required argument 'query'" in (response.error or "")


def test_search_web_is_declared_with_citations_output() -> None:
    spec = get_spec("search_web")
    assert spec is not None
    assert spec["permission"] == "web:search"
    assert spec["read_only"] is True
    assert spec["output"]["required"] == ["count", "results"]
    assert any(t["name"] == "search_web" for t in list_tools())
