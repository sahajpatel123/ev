"""HUD strict-schema surface + explainable digital twin tests."""

from __future__ import annotations

from httpx import AsyncClient

from app.ev.hud import HUD_SCHEMAS, validate_hud


async def post_event(client: AsyncClient, text: str) -> dict:
    resp = await client.post(
        "/v1/events",
        json={"source": "test", "event_type": "note", "text": text},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["event"]


async def seed_pending_alert(client: AsyncClient) -> dict:
    resp = await client.post(
        "/v1/alerts/watchlist",
        json={"kind": "topic", "value": "sqlite", "priority": 0.8},
    )
    assert resp.status_code == 201, resp.text
    await post_event(client, "I decided to use SQLite for local testing.")
    resp = await client.get("/v1/alerts/scan?window_days=7")
    assert resp.status_code == 200, resp.text
    assert resp.json()["alerts_created"]
    return resp.json()["alerts_created"][0]


async def test_hud_alert_schema_endpoint(client: AsyncClient) -> None:
    alert = await seed_pending_alert(client)

    resp = await client.get("/v1/hud/alerts")
    assert resp.status_code == 200, resp.text
    cards = resp.json()
    assert cards
    assert any(card["alert_id"] == alert["id"] for card in cards)

    for card in cards:
        schema_name, validated = validate_hud(card)
        assert schema_name == "ev.hud.alert.v1"
        assert validated.alert_id is not None
        assert 0.0 <= validated.priority <= 1.0
        assert validated.tier in ("urgent", "useful", "background", "notify", "notify_card", "digest")
        assert "fingerprint" in validated.meta


async def test_hud_registry_covers_every_surface(client: AsyncClient) -> None:
    await seed_pending_alert(client)
    await client.post(
        "/v1/focus",
        json={"label": "Ship EV HUD schemas", "kind": "goal"},
    )
    await post_event(client, "I decided to use SQLite for local testing.")

    surfaces = {
        "ev.hud.card.v1": await client.get("/v1/hud/card"),
        "ev.hud.focus.v1": await client.get("/v1/hud/focus"),
        "ev.hud.route.v1": await client.get("/v1/hud/route"),
        "ev.hud.alert.v1": await client.get("/v1/hud/alerts"),
    }
    briefing = await client.post("/v1/tactical/brief", json={"topic": "SQLite", "stakes": "high"})
    assert briefing.status_code == 200, briefing.text
    surfaces["ev.hud.briefing.v1"] = briefing

    for schema_name, resp in surfaces.items():
        assert resp.status_code == 200, f"{schema_name}: {resp.text}"
        payload = resp.json()
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            declared, validated = validate_hud(item)
            assert declared == schema_name
            assert validated.schema_version == schema_name
            assert schema_name in HUD_SCHEMAS


async def test_digital_twin_is_explainable(client: AsyncClient) -> None:
    await post_event(client, "I decided to use SQLite for local testing.")
    await post_event(client, "I want to build EV as a persistent personal AI.")
    await post_event(client, "I prefer local-first storage over cloud-only solutions.")

    resp = await client.get("/v1/twin")
    assert resp.status_code == 200, resp.text
    twin = resp.json()
    items = [*twin["goals"], *twin["preferences"], *twin["facts"]]
    assert items

    for item in items:
        assert item["source_event_ids"], f"twin item lacks provenance: {item}"
        assert item["updated_at"]
        assert item["version"] >= 1

    goal = twin["goals"][0]
    audit = await client.get(f"/v1/audit/{goal['id']}")
    assert audit.status_code == 200, audit.text
    audit_events = {e["id"] for e in audit.json()["source_events"]}
    assert set(goal["source_event_ids"]) <= audit_events
    assert audit_events
