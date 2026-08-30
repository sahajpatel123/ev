"""POL Phase 3/4: life I/O and Home Assistant lights at the real tool entry point."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.training_wheels import TRAINING_STEPS, complete_step
from app.models import AccessLog, Callout, Integration, OwnerTimer
from app.utils.text import utcnow


async def _unlock(db_session: AsyncSession) -> None:
    for step in TRAINING_STEPS:
        await complete_step(db_session, step)
    await db_session.commit()


async def _install(
    db_session: AsyncSession,
    adapter: str,
    *,
    scopes: list[str],
    config: dict | None = None,
    slug: str | None = None,
) -> Integration:
    row = Integration(
        slug=slug or adapter,
        adapter=adapter,
        name=adapter,
        scopes=scopes,
        status="active",
        config=config or {"provider": "local"},
    )
    db_session.add(row)
    await db_session.commit()
    return row


async def test_start_and_cancel_timer_via_gateway(client: AsyncClient, db_session: AsyncSession) -> None:
    started = await client.post(
        "/v1/gateway/tools",
        json={
            "name": "start_timer",
            "arguments": {
                "minutes": 12,
                "text": "stretch",
                "idempotency_key": "timer-stretch-12",
            },
        },
    )
    assert started.status_code == 200, started.text
    body = started.json()
    assert body["ok"] is True, body
    result = body["result"]
    assert result["ok"] is True
    assert result["status"] == "pending"
    assert result["evidence"]["source"] == "owner_timer"
    assert result["evidence"]["accepted"] is True
    timer_id = result["id"]

    replay = await client.post(
        "/v1/gateway/tools",
        json={
            "name": "start_timer",
            "arguments": {
                "minutes": 12,
                "text": "stretch",
                "idempotency_key": "timer-stretch-12",
            },
        },
    )
    assert replay.json()["result"]["id"] == timer_id
    assert replay.json()["result"].get("idempotent_replay") is True

    cancelled = await client.post(
        "/v1/gateway/tools",
        json={"name": "cancel_timer", "arguments": {"id": timer_id}},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["result"]["ok"] is True
    assert cancelled.json()["result"]["status"] == "cancelled"
    row = await db_session.get(OwnerTimer, UUID(timer_id))
    assert row is not None
    assert row.status == "cancelled"

    from app.ev.timers import due_scan

    row.fire_at = utcnow() - timedelta(minutes=1)
    await db_session.commit()
    scanned = await due_scan(db_session)
    assert scanned["fired"] == 0
    leftover = (await db_session.execute(select(Callout))).scalars().all()
    assert not any(str(row.id) == (item.source_item or "") for item in leftover)


async def test_calendar_add_duplicate_and_missing_provider(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    missing = await client.post(
        "/v1/gateway/tools",
        json={
            "name": "calendar_add",
            "arguments": {
                "title": "Dinner",
                "start": "2026-08-21T19:00:00+00:00",
                "end": "2026-08-21T20:00:00+00:00",
                "confirm": True,
            },
        },
    )
    assert missing.status_code == 200, missing.text
    absent = missing.json()["result"]
    assert absent["ok"] is False
    assert absent["error"] == "not_connected"

    await _install(
        db_session,
        "calendar",
        scopes=["calendar:read", "calendar:act"],
        config={"provider": "local", "write": True},
    )
    added = await client.post(
        "/v1/gateway/tools",
        json={
            "name": "calendar_add",
            "arguments": {
                "title": "Dinner",
                "start": "2026-08-21T19:00:00+00:00",
                "end": "2026-08-21T20:00:00+00:00",
                "confirm": True,
            },
        },
    )
    assert added.status_code == 200, added.text
    first = added.json()["result"]
    assert first["ok"] is True, first
    assert first["event_id"]
    assert first["evidence"]["id"] == first["event_id"]
    assert first["evidence"]["accepted"] is True

    again = await client.post(
        "/v1/gateway/tools",
        json={
            "name": "calendar_add",
            "arguments": {
                "title": "Dinner",
                "start": "2026-08-21T19:00:00+00:00",
                "end": "2026-08-21T20:00:00+00:00",
                "confirm": True,
            },
        },
    )
    second = again.json()["result"]
    assert second["ok"] is True
    assert second["event_id"] == first["event_id"]
    assert second.get("duplicate") or second.get("idempotent_replay")


async def test_place_call_not_connected_and_named_confirmation(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _unlock(db_session)
    missing = await client.post(
        "/v1/gateway/tools",
        json={
            "name": "place_call",
            "arguments": {"name": "Ned", "confirm": True},
            "allow_sensitive": True,
        },
    )
    assert missing.status_code == 200, missing.text
    body = missing.json()
    result = body.get("result") or {}
    assert result.get("error") == "not_connected"

    await _unlock(db_session)
    await _install(db_session, "phone", scopes=["phone:act"], config={"provider": "local"})
    voice = await client.post(
        "/v1/gateway/tools",
        json={
            "name": "place_call",
            "arguments": {"name": "Ned", "confirm": True},
            "allow_sensitive": True,
        },
    )
    # HTTP master key + confirm is an independent factor; local double does not open.
    acted = voice.json()
    payload = acted.get("result") or {}
    if acted.get("ok") and payload.get("ok"):
        assert payload.get("opened") is True
        assert payload.get("evidence", {}).get("opened") is True
        assert "Ned" in (payload.get("spoken") or "")
    else:
        assert payload.get("opened") is not True
        assert payload.get("error") in {"not_opened", "not_connected", "not_available", None}


async def test_place_call_rings_only_with_opened_evidence(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    from app.integrations import service as integrations

    await _unlock(db_session)
    await _install(db_session, "phone", scopes=["phone:act"], config={"provider": "http"})

    async def opened_action(session, integration_id, action, args, *, actor):
        return SimpleNamespace(
            result={
                "opened": True,
                "mode": "http",
                "data": {"opened": True},
                "delivery": {"evidence": {"opened": True, "source": "http"}},
            }
        )

    monkeypatch.setattr(integrations, "execute_action", opened_action)
    ringing = await client.post(
        "/v1/gateway/tools",
        json={
            "name": "place_call",
            "arguments": {"name": "Ned", "kind": "tel", "confirm": True},
            "allow_sensitive": True,
        },
    )
    assert ringing.status_code == 200, ringing.text
    payload = ringing.json()["result"]
    assert payload["ok"] is True
    assert payload["opened"] is True
    assert payload["evidence"]["opened"] is True
    assert payload["evidence"]["name"] == "Ned"
    assert payload["spoken"].startswith("Ringing ")


async def test_mail_drafts_by_default_and_send_needs_evidence(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _unlock(db_session)
    drafted = await client.post(
        "/v1/gateway/tools",
        json={
            "name": "draft_reply",
            "arguments": {"mail_id": "m-life", "body": "Thanks", "to": "mom@example.com"},
            "allow_sensitive": True,
        },
    )
    assert drafted.status_code == 200, drafted.text
    draft = drafted.json()["result"]
    assert draft["ok"] is True
    assert draft["sent"] is False
    assert draft["evidence"]["sent"] is False

    blocked = await client.post(
        "/v1/gateway/tools",
        json={
            "name": "draft_reply",
            "arguments": {"mail_id": "m-life", "send": True},
            "allow_sensitive": True,
        },
    )
    assert blocked.json()["result"]["sent"] is not True

    await _install(
        db_session,
        "mail",
        scopes=["mail:read", "mail:act"],
        config={"provider": "local"},
    )
    sent = await client.post(
        "/v1/gateway/tools",
        json={
            "name": "draft_reply",
            "arguments": {"mail_id": "m-life", "send": True, "confirm": True},
            "allow_sensitive": True,
        },
    )
    payload = sent.json()["result"]
    assert payload["sent"] is True
    assert payload["evidence"]["sent"] is True
    assert payload["evidence"]["observed"] is True
    assert payload.get("simulated") or payload["evidence"].get("simulated")
    assert "local" in (payload["spoken"] or "").lower() or payload["evidence"].get("source") == "local"

    listed = await client.post(
        "/v1/gateway/tools",
        json={"name": "list_mail", "arguments": {"limit": 5}},
    )
    assert listed.status_code == 200, listed.text
    inbox = listed.json()["result"]
    assert inbox.get("error") != "not_connected"
    assert inbox.get("ok") is True


async def test_digest_reports_missing_sources_without_fake_success(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    plate = await client.post("/v1/gateway/tools", json={"name": "whats_on_my_plate", "arguments": {}})
    assert plate.status_code == 200, plate.text
    body = plate.json()["result"]
    assert body["ok"] is True
    assert body["calendar_only"] is True
    assert body["sources"]["mail"]["error"] == "not_connected"
    assert body["sources"]["github"]["error"] == "not_connected"
    assert body["evidence"]["source"] == "digest"

    await _install(
        db_session,
        "github",
        scopes=["github:read"],
        config={
            "provider": "local",
            "repo": "owner/ev",
            "issues": [{"number": 1, "title": "Ship lights"}],
        },
    )
    await _install(
        db_session,
        "calendar",
        scopes=["calendar:read"],
        config={"provider": "local"},
        slug="calendar-digest",
    )
    filled = await client.post("/v1/gateway/tools", json={"name": "whats_on_my_plate", "arguments": {}})
    digest = filled.json()["result"]
    assert digest["sources"]["github"]["ok"] is True
    assert digest["github"]
    assert digest["evidence"]["sources"]["github"]["ok"] is True


async def test_home_light_local_double_and_ha_provider_failure(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    await _unlock(db_session)
    local = await client.post(
        "/v1/gateway/tools",
        json={
            "name": "home_act",
            "arguments": {"entity": "lab lights", "action": "on", "confirm": True},
            "allow_sensitive": True,
        },
    )
    assert local.status_code == 200, local.text
    turned = local.json()["result"]
    assert turned["ok"] is True, turned
    assert turned["new_state"] == "on"
    assert turned["evidence"]["accepted"] is True
    assert turned["evidence"]["observed"] is True
    assert turned["simulated"] is True

    again = await client.post(
        "/v1/gateway/tools",
        json={
            "name": "home_act",
            "arguments": {"entity": "lab lights", "action": "on", "confirm": True},
            "allow_sensitive": True,
        },
    )
    assert again.json()["result"].get("idempotent_replay") is True

    installed = await client.post(
        "/v1/integrations",
        json={
            "adapter": "smart_home",
            "name": "Lab HA",
            "scopes": ["home:read", "home:act"],
            "config": {"provider": "homeassistant"},
        },
    )
    assert installed.status_code == 201, installed.text
    disconnected = await client.post(
        "/v1/gateway/tools",
        json={
            "name": "home_act",
            "arguments": {"entity": "lab lights", "action": "off", "confirm": True},
            "allow_sensitive": True,
        },
    )
    failed = disconnected.json()["result"]
    assert failed["ok"] is False
    assert failed["error"] == "not_connected"


async def test_homeassistant_timeout_is_not_success(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    await _unlock(db_session)
    installed = await client.post(
        "/v1/integrations",
        json={
            "adapter": "smart_home",
            "name": "Lab HA timeout",
            "scopes": ["home:read", "home:act"],
            "config": {"provider": "homeassistant", "base_url": "http://ha.example:8123"},
        },
    )
    assert installed.status_code == 201, installed.text
    integration_id = installed.json()["id"]
    stored = await client.post(
        f"/v1/integrations/{integration_id}/credentials",
        json={"access_token": "ha-timeout-token"},
    )
    assert stored.status_code in {200, 201}, stored.text

    async def live_off(*_a, **_k):
        return {"state": "off", "raw": {}, "accepted": True}

    async def hang(*_a, **_k):
        await asyncio.sleep(5)
        return {"state": "on", "accepted": True}

    monkeypatch.setattr("app.ev.home._ha_get_state", live_off)
    monkeypatch.setattr("app.ev.home._ha_act", hang)
    monkeypatch.setattr("app.ev.home.DEFAULT_TIMEOUT_SECONDS", 0.05)
    timed = await client.post(
        "/v1/gateway/tools",
        json={
            "name": "home_act",
            "arguments": {"entity": "lab lights", "action": "on", "confirm": True},
            "allow_sensitive": True,
        },
    )
    payload = timed.json()["result"]
    assert payload["ok"] is False
    assert payload["error"] == "timeout"
    assert "claim" in (payload.get("spoken") or "").lower() or "timed out" in (payload.get("spoken") or "").lower()


async def test_life_io_writes_audit_records(client: AsyncClient, db_session: AsyncSession) -> None:
    started = await client.post(
        "/v1/gateway/tools",
        json={"name": "start_timer", "arguments": {"minutes": 3, "text": "audit me"}},
    )
    assert started.json()["ok"] is True
    rows = list(
        (await db_session.execute(select(AccessLog).where(AccessLog.action == "life_act"))).scalars().all()
    )
    assert rows
    assert any(row.resource_type == "start_timer" for row in rows)


async def test_ambiguous_lights_do_not_act(client: AsyncClient, db_session: AsyncSession) -> None:
    from app.ev.home import ensure_inventory
    from app.models import HomeEntity

    await _unlock(db_session)
    await ensure_inventory(db_session)
    db_session.add(
        HomeEntity(
            entity_id="light.kitchen",
            name="kitchen lights",
            area="kitchen",
            domain="light",
            state="off",
            attributes={},
            updated_at=utcnow(),
        )
    )
    await db_session.commit()
    acted = await client.post(
        "/v1/gateway/tools",
        json={
            "name": "home_act",
            "arguments": {"entity": "lights", "action": "on", "confirm": True},
            "allow_sensitive": True,
        },
    )
    assert acted.status_code == 200, acted.text
    payload = acted.json()["result"]
    assert payload["ok"] is False
    assert payload["error"] == "ambiguous"
    assert len(payload.get("candidates") or []) >= 2


async def test_home_observes_live_state_before_acting(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    await _unlock(db_session)
    installed = await client.post(
        "/v1/integrations",
        json={
            "adapter": "smart_home",
            "name": "Lab HA observe",
            "scopes": ["home:read", "home:act"],
            "config": {"provider": "homeassistant", "base_url": "http://ha.example:8123"},
        },
    )
    assert installed.status_code == 201, installed.text
    integration_id = installed.json()["id"]
    stored = await client.post(
        f"/v1/integrations/{integration_id}/credentials",
        json={"access_token": "ha-observe-token"},
    )
    assert stored.status_code in {200, 201}, stored.text
    acted = {"called": False}

    async def live_on(*_a, **_k):
        return {"state": "on", "raw": {"state": "on"}, "accepted": True}

    async def should_not_act(*_a, **_k):
        acted["called"] = True
        return {"state": "on", "accepted": True}

    monkeypatch.setattr("app.ev.home._ha_get_state", live_on)
    monkeypatch.setattr("app.ev.home._ha_act", should_not_act)
    result = await client.post(
        "/v1/gateway/tools",
        json={
            "name": "home_act",
            "arguments": {"entity": "lab lights", "action": "on", "confirm": True},
            "allow_sensitive": True,
        },
    )
    payload = result.json()["result"]
    assert payload["ok"] is True, payload
    assert payload["idempotent_replay"] is True
    assert payload["evidence"]["observed_state"] == "on"
    assert acted["called"] is False


async def test_home_status_does_not_claim_unobserved_ha(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _unlock(db_session)
    installed = await client.post(
        "/v1/integrations",
        json={
            "adapter": "smart_home",
            "name": "Lab HA missing",
            "scopes": ["home:read", "home:act"],
            "config": {"provider": "homeassistant"},
        },
    )
    assert installed.status_code == 201, installed.text
    status = await client.post("/v1/gateway/tools", json={"name": "home_status", "arguments": {}})
    assert status.status_code == 200, status.text
    payload = status.json()["result"]
    assert payload["ok"] is True
    assert payload["evidence"]["observed"] is False
    assert payload.get("error") == "not_connected"
    assert payload.get("stale") is True


async def test_calendar_near_duplicate_window(client: AsyncClient, db_session: AsyncSession) -> None:
    await _install(
        db_session,
        "calendar",
        scopes=["calendar:read", "calendar:act"],
        config={"provider": "local", "write": True},
    )
    first = await client.post(
        "/v1/gateway/tools",
        json={
            "name": "calendar_add",
            "arguments": {
                "title": "Dinner",
                "start": "2026-08-21T19:00:00+00:00",
                "end": "2026-08-21T20:00:00+00:00",
                "confirm": True,
            },
        },
    )
    assert first.json()["result"]["ok"] is True, first.json()
    near = await client.post(
        "/v1/gateway/tools",
        json={
            "name": "calendar_add",
            "arguments": {
                "title": "dinner",
                "start": "2026-08-21T19:10:00+00:00",
                "end": "2026-08-21T20:10:00+00:00",
                "confirm": True,
            },
        },
    )
    payload = near.json()["result"]
    assert payload["ok"] is True
    assert payload.get("duplicate") or payload.get("idempotent_replay")
    assert payload["event_id"] == first.json()["result"]["event_id"]


async def test_list_and_snooze_timer(client: AsyncClient, db_session: AsyncSession) -> None:
    started = await client.post(
        "/v1/gateway/tools",
        json={"name": "start_timer", "arguments": {"at": "in 20 minutes", "text": "stretch"}},
    )
    assert started.status_code == 200, started.text
    body = started.json()["result"]
    assert body["ok"] is True, body
    listed = await client.post("/v1/gateway/tools", json={"name": "list_timers", "arguments": {}})
    assert listed.json()["result"]["count"] >= 1
    snoozed = await client.post(
        "/v1/gateway/tools",
        json={"name": "snooze_timer", "arguments": {"id": body["id"], "minutes": 8}},
    )
    assert snoozed.json()["result"]["ok"] is True
    assert snoozed.json()["result"]["id"] == body["id"]


async def test_set_reminder_parses_relative_when(client: AsyncClient, db_session: AsyncSession) -> None:
    from app.models import OwnerTimer

    reminded = await client.post(
        "/v1/gateway/tools",
        json={"name": "set_reminder", "arguments": {"text": "drink water in 10 minutes"}},
    )
    assert reminded.status_code == 200, reminded.text
    payload = reminded.json()["result"]
    assert payload["ok"] is True
    assert payload.get("stored") != "alert"
    row = await db_session.get(OwnerTimer, UUID(payload["id"]))
    assert row is not None
    assert row.status == "pending"


async def test_digest_ranks_due_timer_first(client: AsyncClient, db_session: AsyncSession) -> None:
    await client.post(
        "/v1/gateway/tools",
        json={"name": "start_timer", "arguments": {"minutes": 2, "text": "stand up"}},
    )
    plate = await client.post("/v1/gateway/tools", json={"name": "whats_on_my_plate", "arguments": {}})
    body = plate.json()["result"]
    assert body["ok"] is True
    assert body["priority"]
    assert body["priority"][0]["kind"] == "timer"
    assert "stand up" in (body["priority"][0]["title"] or "")


async def test_place_call_ambiguous_contacts(client: AsyncClient, db_session: AsyncSession) -> None:
    await _unlock(db_session)
    await _install(db_session, "phone", scopes=["phone:act"], config={"provider": "local"})
    await _install(
        db_session,
        "contacts",
        scopes=["contacts:read"],
        config={
            "provider": "local",
            "contacts": [
                {"name": "Ned Smith", "phone": "+15551111111"},
                {"name": "Ned Jones", "phone": "+15552222222"},
            ],
        },
    )
    called = await client.post(
        "/v1/gateway/tools",
        json={
            "name": "place_call",
            "arguments": {"name": "Ned", "confirm": True},
            "allow_sensitive": True,
        },
    )
    payload = called.json().get("result") or {}
    assert payload.get("error") == "ambiguous"
    assert payload.get("opened") is not True
    assert len(payload.get("candidates") or []) >= 2


async def test_webhook_state_updates_owned_light(db_session: AsyncSession) -> None:
    from app.ev.home import apply_observed_updates, ensure_inventory, resolve_entity

    await ensure_inventory(db_session)
    updated = await apply_observed_updates(
        db_session,
        [
            {
                "event_type": "home.device.updated",
                "payload": {"entity_id": "light.lab", "state": "on"},
            }
        ],
    )
    assert updated == 1
    row = await resolve_entity(db_session, "lab lights")
    assert row is not None
    assert row.state == "on"


async def test_start_timer_one_minute_speaks_duration_not_iso(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    started = await client.post(
        "/v1/gateway/tools",
        json={"name": "start_timer", "arguments": {"minutes": 1, "text": "stretch"}},
    )
    assert started.status_code == 200, started.text
    body = started.json()["result"]
    assert body["ok"] is True
    assert body["spoken"] == "Timer set for one minute."


async def test_due_scan_is_idempotent_for_one_timer(db_session: AsyncSession) -> None:
    from app.ev.timers import due_scan, start_timer

    started = await start_timer(db_session, minutes=1, text="ring")
    row = await db_session.get(OwnerTimer, UUID(started["id"]))
    assert row is not None
    row.fire_at = utcnow() - timedelta(seconds=1)
    await db_session.commit()
    first = await due_scan(db_session)
    second = await due_scan(db_session)
    assert first["fired"] == 1
    assert second["fired"] == 0
