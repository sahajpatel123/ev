"""Citation-aware search result parsing (plan 11.3)."""

from __future__ import annotations

from app.integrations.adapters import BUILTIN_ADAPTERS, normalize_search_results


def test_normalize_search_results_canonical_shape() -> None:
    raw = [
        {
            "url": "https://example.com/a",
            "title": "Alpha",
            "snippet": "about alpha",
            "published": "2026-08-01",
        },
        {"link": "https://example.com/b", "name": "Beta", "description": "about beta"},
        {"href": "javascript:alert(1)", "title": "unsafe"},
        {"url": "ftp://example.com/x", "title": "no http"},
        "not-a-dict",
    ]
    out = normalize_search_results(raw)
    assert out["result_count"] == 2
    assert out["results"][0] == {
        "title": "Alpha",
        "url": "https://example.com/a",
        "snippet": "about alpha",
        "published_at": "2026-08-01",
    }
    assert out["results"][1]["title"] == "Beta"
    assert out["results"][1]["url"] == "https://example.com/b"
    assert out["citations"] == [
        {"index": 1, "title": "Alpha", "url": "https://example.com/a"},
        {"index": 2, "title": "Beta", "url": "https://example.com/b"},
    ]


def test_normalize_search_results_bounds_and_sanitizes() -> None:
    raw = [
        {
            "url": f"https://example.com/{i}",
            "title": "x" * 2000,
            "snippet": "y" * 2000,
        }
        for i in range(30)
    ]
    out = normalize_search_results(raw)
    assert out["result_count"] == 20
    assert all(len(result["title"]) <= 500 for result in out["results"])
    assert all(len(result["snippet"]) <= 500 for result in out["results"])
    assert len(out["citations"]) == 20


async def test_search_adapter_local_mode_returns_cited_shape() -> None:
    search = next(adapter for adapter in BUILTIN_ADAPTERS if adapter.slug == "search")
    result = await search.act(
        action="search.query",
        args={"query": "EV memory"},
        token="",
        scopes=["search:read"],
        config={},
    )
    assert result["ok"] is True
    assert result["mode"] == "local"
    assert result["results"] == []
    assert result["citations"] == []
    assert result["result_count"] == 0
