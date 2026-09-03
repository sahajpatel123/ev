"""Tests for permissioned web search (plan 11.3): provider seam + tool dispatch."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.ev.research import ResearchService
from app.ev.tools import dispatch, get_spec, list_tools
from app.schemas import ResearchSessionCreate
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

    monkeypatch.setattr(settings, "search_provider", "live")
    from app.search.live import LiveSearchProvider

    assert isinstance(get_search_provider(), LiveSearchProvider)


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
    assert response.result["spoken"]


async def test_computer_web_research_dispatches_search_web(
    db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "search_provider", "mock")
    response = await dispatch(
        db_session,
        "computer",
        {"goal": "search the web for AI and Machine Learning for Coders"},
    )
    assert response.ok is True, response.error
    assert response.result is not None
    assert response.result.get("count", 0) >= 1
    assert "results" in response.result
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
    # Capability-theater contract: honest degraded result with an exact next_step.
    assert response.ok is True
    assert response.result is not None
    assert response.result["degraded"] is True
    assert "EV_SEARCH_PROVIDER" in response.result["next_step"]
    assert response.result["count"] == 0


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


async def test_research_web_search_adds_cited_notes(
    db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "search_provider", "mock")
    service = ResearchService(db_session, actor="master")
    session = await service.create_session(
        ResearchSessionCreate(question="Which local embedding model is best?")
    )
    notes = await service.web_search(session.id, "local embedding benchmark", limit=2)
    assert len(notes) == 2
    assert all(note.source_url and note.source_title for note in notes)
    assert all(note.note for note in notes)
    await db_session.commit()


async def test_research_web_search_memory_only_mode_raises(
    db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "search_provider", "none")
    service = ResearchService(db_session, actor="tester")
    session = await service.create_session(
        ResearchSessionCreate(question="Memory-only question")
    )
    try:
        await service.web_search(session.id, "anything")
        raise AssertionError("expected KeyError for disabled web search")
    except KeyError as exc:
        assert "disabled" in str(exc)
