"""AGENT 7 ROSTER: licensed, attributed public-figure biodata resolution + cache."""

from __future__ import annotations

import urllib.parse

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Entity, PublicFigureCache
from app.people.biodata import BiodataError, BiodataResolver

PUBLIC_FIGURES = [
    "Ada Lovelace",
    "Marie Curie",
    "Alan Turing",
    "Rosa Parks",
    "Albert Einstein",
    "Frida Kahlo",
    "Nelson Mandela",
    "Jane Austen",
    "Nikola Tesla",
    "Katherine Johnson",
]


def _item_body(name: str, qid: str) -> dict:
    return {
        "head": {"vars": ["item", "itemLabel", "birth", "death"]},
        "results": {
            "bindings": [
                {
                    "item": {"type": "uri", "value": f"http://www.wikidata.org/entity/{qid}"},
                    "itemLabel": {"type": "literal", "value": name},
                    "birth": {"type": "literal", "value": "1900-01-01T00:00:00Z"},
                    "death": {"type": "literal", "value": "1970-01-01T00:00:00Z"},
                }
            ]
        },
    }


def _summary_body(title: str) -> dict:
    return {
        "extract": f"{title} is a public figure used to test attributed biodata.",
        "content_urls": {
            "desktop": {"page": f"https://en.wikipedia.org/wiki/{urllib.parse.quote(title)}"}
        },
    }


def mock_transport(calls: dict | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls["requests"] = calls.get("requests", 0) + 1
        if "query.wikidata.org" in str(request.url):
            query = request.url.params.get("query", "")
            name = query.split('"')[1]
            qid = f"Q{abs(hash(name)) % 9000 + 1}"
            if "wdt:P106" in query:
                return httpx.Response(
                    200,
                    json={
                        "head": {"vars": ["label"]},
                        "results": {
                            "bindings": [{"label": {"value": "scientist"}}]
                        },
                    },
                )
            if "wdt:P800" in query:
                return httpx.Response(
                    200,
                    json={
                        "head": {"vars": ["label"]},
                        "results": {
                            "bindings": [{"label": {"value": "notable work"}}]
                        },
                    },
                )
            return httpx.Response(200, json=_item_body(name, qid))
        if "en.wikipedia.org" in str(request.url):
            title = urllib.parse.unquote(request.url.path.rsplit("/", 1)[-1])
            return httpx.Response(200, json=_summary_body(title))
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(handler)


async def _resolver(
    db_session: AsyncSession,
    *,
    calls: dict | None = None,
) -> tuple[BiodataResolver, AsyncClient]:
    client = AsyncClient(transport=mock_transport(calls))
    return BiodataResolver(db_session, client=client), client


async def test_resolves_ten_public_figures_with_attribution(
    db_session: AsyncSession,
) -> None:
    resolver, client = await _resolver(db_session)
    try:
        for name in PUBLIC_FIGURES:
            result = await resolver.resolve(name)
            out = await resolver.to_schema(result)
            assert out.name == name
            assert len(out.occupations) >= 1
            assert len(out.notable_works) >= 1
            assert out.dates is not None
            assert out.summary is not None
            for field in [*out.occupations, *out.notable_works]:
                assert field.source_url.startswith("https://www.wikidata.org/wiki/")
                assert field.license == "CC0"
            assert out.summary.license == "CC BY-SA 4.0"
            assert out.summary.source_url.startswith("https://en.wikipedia.org/wiki/")
            assert out.source_url.startswith("https://www.wikidata.org/wiki/")
            assert out.degraded is False
            assert out.merged is False
    finally:
        await client.aclose()


async def test_biodata_cache_serves_fresh_rows_without_refetch(
    db_session: AsyncSession,
) -> None:
    calls: dict = {}
    resolver1, client1 = await _resolver(db_session, calls=calls)
    try:
        first = await resolver1.resolve("Ada Lovelace")
        assert first.cached is False
        await client1.aclose()

        resolver2, client2 = await _resolver(db_session, calls=calls)
        try:
            second = await resolver2.resolve("Ada Lovelace")
            assert second.cached is True
            assert second.occupations == first.occupations
        finally:
            await client2.aclose()
        assert calls["requests"] == 4  # 3 SPARQL (item/P106/P800) + 1 Wikipedia REST

        resolver3, client3 = await _resolver(db_session, calls=calls)
        try:
            refreshed = await resolver3.resolve("Ada Lovelace", refresh=True)
            assert refreshed.cached is False
        finally:
            await client3.aclose()
        assert calls["requests"] == 8
    finally:
        await client1.aclose()


async def test_biodata_link_requires_explicit_confirmation(
    db_session: AsyncSession,
) -> None:
    entity = Entity(
        name="Ada Lovelace",
        entity_type="person",
        canonical_key="person:ada lovelace",
    )
    db_session.add(entity)
    await db_session.flush()

    resolver, client = await _resolver(db_session)
    try:
        result = await resolver.resolve("Ada Lovelace")
        assert result.merged is False

        row = await resolver.link_to_entity(
            "Ada Lovelace",
            entity.id,
            actor="test",
            reason="explicit owner confirmation",
        )
        assert row.confirmed is True
        assert row.entity_id == entity.id

        linked = await resolver.resolve("Ada Lovelace")
        assert linked.merged is True
    finally:
        await client.aclose()


async def test_delete_for_entity_removes_cached_biodata(
    db_session: AsyncSession,
) -> None:
    entity = Entity(
        name="Marie Curie",
        entity_type="person",
        canonical_key="person:marie curie",
    )
    db_session.add(entity)
    await db_session.flush()

    resolver, client = await _resolver(db_session)
    try:
        await resolver.resolve("Marie Curie")
        await resolver.link_to_entity("Marie Curie", entity.id, actor="test")
        deleted = await resolver.delete_for_entity(entity.id)
        assert deleted == 1
        remaining = await db_session.scalar(
            select(func.count())
            .select_from(PublicFigureCache)
            .where(PublicFigureCache.entity_id == entity.id)
        )
        assert remaining == 0
    finally:
        await client.aclose()


async def test_biodata_disabled_returns_503(
    client: AsyncClient, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "biodata_provider", "none")
    resp = await client.get("/v1/people/Ada%20Lovelace/biodata")
    assert resp.status_code == 503
    assert resp.headers.get("x-error-code") == "biodata_disabled"


async def test_biodata_not_found_raises_404(db_session: AsyncSession) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    client = AsyncClient(transport=httpx.MockTransport(handler))
    resolver = BiodataResolver(db_session, client=client)
    try:
        with pytest.raises(BiodataError) as excinfo:
            await resolver.resolve("Nobody Real")
        assert excinfo.value.status == 404
    finally:
        await client.aclose()
