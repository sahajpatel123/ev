"""Tactical quick-card cache tests (FR-TACTICAL-03: quick card < 800 ms)."""

from __future__ import annotations

import time

from httpx import AsyncClient

from app.ev.hud import validate_hud


async def post_event(client: AsyncClient, text: str) -> None:
    resp = await client.post(
        "/v1/events",
        json={"source": "test", "event_type": "note", "text": text},
    )
    assert resp.status_code == 201, resp.text


async def seed_decisions(client: AsyncClient) -> None:
    await post_event(client, "I decided to use SQLite for local testing.")
    await post_event(client, "I want to build EV as a persistent personal AI.")


async def test_prepare_then_cached_read(client: AsyncClient) -> None:
    await seed_decisions(client)

    resp = await client.post(
        "/v1/tactical/prepare",
        json={"topic": "SQLite", "stakes": "high"},
    )
    assert resp.status_code == 200, resp.text
    prepared = resp.json()
    assert prepared["schema_version"] == "ev.hud.quickcard.v1"
    assert prepared["objective"] == "SQLite"
    assert prepared["meta"]["source"] == "fresh"

    # Cached read: same objective, no rebuild, hit counter advances.
    resp = await client.get("/v1/tactical/quick?topic=SQLite")
    assert resp.status_code == 200, resp.text
    cached = resp.json()
    assert cached["objective"] == "SQLite"
    assert cached["meta"]["source"] == "cache"
    assert cached["meta"]["hit_count"] == 2

    schema_name, validated = validate_hud(cached)
    assert schema_name == "ev.hud.quickcard.v1"
    assert validated.summary
    assert validated.options_count >= 1


async def test_quick_card_rebuilds_when_expired(client: AsyncClient) -> None:
    await seed_decisions(client)

    # ttl_seconds=0 disables the cache read, so every call rebuilds fresh.
    first = await client.get("/v1/tactical/quick?topic=SQLite&ttl_seconds=0")
    assert first.status_code == 200, first.text
    assert first.json()["meta"]["source"] == "fresh"

    second = await client.get("/v1/tactical/quick?topic=SQLite&ttl_seconds=0")
    assert second.status_code == 200
    assert second.json()["meta"]["source"] == "fresh"

    # And a prepared card is ignored when the read forces expiry.
    await client.post("/v1/tactical/prepare", json={"topic": "SQLite", "ttl_seconds": 3600})
    expired = await client.get("/v1/tactical/quick?topic=SQLite&ttl_seconds=0")
    assert expired.json()["meta"]["source"] == "fresh"


async def test_prepare_is_ledgered(client: AsyncClient) -> None:
    await seed_decisions(client)

    resp = await client.post("/v1/tactical/prepare", json={"topic": "SQLite"})
    assert resp.status_code == 200, resp.text

    commands = (await client.get("/v1/commands")).json()
    prepare = next(c for c in commands if c["command_type"] == "tactical.quickcard.prepare")
    assert prepare["actor"] == "master"
    assert prepare["status"] == "completed"
    assert prepare["target_type"] == "topic"
    assert prepare["request"]["topic"] == "SQLite"


def _p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return ordered[index]


async def test_quickcard_and_briefing_p95_meet_documented_budgets(
    client: AsyncClient,
) -> None:
    """Measured on this Mac (Apple M2): p95 quick card <= 800 ms, briefing <= 3000 ms."""
    await seed_decisions(client)
    await client.post("/v1/tactical/prepare", json={"topic": "SQLite"})

    quick_samples: list[float] = []
    for _ in range(30):
        tick = time.perf_counter()
        resp = await client.get("/v1/tactical/quick?topic=SQLite")
        assert resp.status_code == 200, resp.text
        quick_samples.append((time.perf_counter() - tick) * 1000)

    brief_samples: list[float] = []
    for _ in range(15):
        tick = time.perf_counter()
        resp = await client.post(
            "/v1/tactical/brief",
            json={"topic": "SQLite", "stakes": "high"},
        )
        assert resp.status_code == 200, resp.text
        brief_samples.append((time.perf_counter() - tick) * 1000)

    quick_p95 = _p95(quick_samples)
    brief_p95 = _p95(brief_samples)
    assert quick_p95 <= 800.0, f"quickcard p95={quick_p95:.1f}ms > 800ms ({quick_samples})"
    assert brief_p95 <= 3000.0, f"briefing p95={brief_p95:.1f}ms > 3000ms ({brief_samples})"
