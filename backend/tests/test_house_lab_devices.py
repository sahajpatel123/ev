"""House/lab/devices (items 11–25): shipped dispatcher, adapters, routing, gates."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev import fleet, home, timers, travel, workshop
from app.ev import tools as ev_tools
from app.ev.calendar_write import ticket_buy
from app.ev.delegates import grant
from app.ev.training_wheels import (
    TRAINING_STEPS,
    complete_step,
    ensure_seed_gates,
    unlock_after_training,
)
from app.ev.voice_life import place_call
from app.integrations.adapters import registry
from app.integrations.life_helper import EXIT_NOT_AVAILABLE, LifeHelperError
from app.models import (
    BomItem,
    Callout,
    Device,
    FeatureGate,
    GearSnapshot,
    Integration,
    MakerProject,
    OwnerTimer,
    VoiceSession,
)
from app.notify.routing import best_reachable_device
from app.utils.text import sha256_hex, utcnow


async def _unlock(session: AsyncSession) -> None:
    await ensure_seed_gates(session)
    await unlock_after_training(session)
    await session.commit()


async def test_tts_playback_uses_heartbeat_attention_or_voice(db_session: AsyncSession) -> None:
    now = utcnow()
    only = Device(
        name="Mac",
        capabilities=["attention", "voice"],
        last_seen_at=now,
        token_hash="a" * 64,
    )
    db_session.add(only)
    await db_session.commit()
    picked = await fleet.tts_playback_device(db_session, now=now)
    assert picked is not None
    assert picked.id == only.id
    assert await best_reachable_device(db_session, "attention", now=now) == only


async def test_runtime_utterance_includes_routed_tts_device(client: AsyncClient) -> None:
    from tests.test_runtime import (
        _verified_runtime_session,
        enroll_owner,
        grant_voice_consent,
        register_device,
        heartbeat,
    )

    await grant_voice_consent(client)
    await enroll_owner(client)
    other = await register_device(client, "stale-phone", capabilities=["voice"])
    await heartbeat(client, str(other["id"]))
    outcome = await _verified_runtime_session(client, "mac-attention")
    # Heartbeat on the wake device is already posted by _verified_runtime_session.
    spoken = await client.post(
        "/v1/runtime/utterance",
        json={"session_id": outcome["session_id"], "text": "hello from the shop"},
    )
    assert spoken.status_code == 200, spoken.text
    body = spoken.json()
    devices = (await client.get("/v1/devices")).json()
    ids = {row["id"] for row in devices}
    assert body["tts_device_id"] in ids
    routed = next(row for row in devices if row["id"] == body["tts_device_id"])
    caps = {str(c).lower() for c in (routed.get("capabilities") or [])}
    assert caps & {"voice", "attention"}


async def test_single_online_device_is_the_tts_fallback(db_session: AsyncSession) -> None:
    now = utcnow()
    phone = Device(
        name="Phone",
        capabilities=["voice"],
        last_seen_at=now,
        token_hash="b" * 64,
    )
    db_session.add(phone)
    await db_session.commit()
    picked = await fleet.tts_playback_device(db_session, now=now)
    assert picked is not None
    assert picked.id == phone.id


async def test_bootstrap_speaks_once_and_returns_owner_prefs(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    created = await client.post(
        "/v1/devices",
        json={"name": "Phone A", "capabilities": ["attention", "voice"]},
    )
    assert created.status_code == 201, created.text
    device_id = created.json()["device"]["id"]

    first = await client.get(f"/v1/devices/{device_id}/bootstrap")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["spoken"] is True
    assert body["spoken_text"] == "We're online."
    prefs = body["prefs"]
    assert prefs["nickname"]
    assert "quiet_hours" in prefs
    assert "feature_gates" in prefs
    assert prefs.get("tts_voice")

    second = await client.get(f"/v1/devices/{device_id}/bootstrap")
    assert second.status_code == 200, second.text
    assert second.json()["spoken"] is False
    row = await db_session.get(Device, UUID(device_id))
    assert row is not None
    assert row.bootstrapped_spoken_at is not None


async def test_transcript_streams_owner_live_thread(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    chat = await client.post("/v1/chat", json={"message": "hello from the lookout", "stream": False})
    assert chat.status_code == 200, chat.text
    conversation_id = chat.json()["conversation_id"]
    listed = await client.get("/v1/runtime/transcript")
    assert listed.status_code == 200, listed.text
    payload = listed.json()
    assert payload["conversation_id"] == conversation_id
    texts = [item["text"] for item in payload["events"]]
    assert any("hello from the lookout" in text for text in texts)


async def test_home_act_matches_state_and_fails_on_mismatch(db_session: AsyncSession) -> None:
    await _unlock(db_session)
    ok = await ev_tools.dispatch(
        db_session,
        "home_act",
        {"entity": "lab lights", "action": "on", "confirm": True},
        actor="master",
        allow_sensitive=True,
    )
    assert ok.ok is True, ok.error
    assert ok.result is not None
    assert ok.result["ok"] is True
    assert ok.result["new_state"] == "on"
    assert "simulated home" in ok.result["spoken"].lower()

    mismatch = await home.home_act(
        db_session,
        "lab lights",
        "off",
        confirm=True,
        actor="master",
        config={"provider": "local", "simulate_mismatch": True},
    )
    assert mismatch["ok"] is False
    assert mismatch["error"] == "state_mismatch"


async def test_home_status_identifies_simulated_home(db_session: AsyncSession) -> None:
    status = await ev_tools.dispatch(db_session, "home_status", {}, actor="master")
    assert status.ok is True
    assert status.result is not None
    assert status.result["simulated"] is True
    assert "simulated home" in status.result["spoken"].lower()


async def test_homeassistant_adapter_local_double_matching_state() -> None:
    adapter = registry.get("smart_home")
    assert adapter is not None
    config: dict = {"provider": "local", "house": {}}
    turned = await adapter.act(
        action="light.set",
        args={"entity": "lab lights", "action": "on"},
        token="",
        scopes=["home:act"],
        config=config,
    )
    assert turned["ok"] is True
    assert turned["new_state"] == "on"
    broken = await adapter.act(
        action="light.set",
        args={"entity": "lab lights", "action": "off", "force_mismatch": True},
        token="",
        scopes=["home:act"],
        config=config,
    )
    assert broken["ok"] is False
    assert broken["error"] == "state_mismatch"


async def test_place_call_rings_only_when_opened(
    db_session: AsyncSession, monkeypatch
) -> None:
    await _unlock(db_session)
    from app.integrations import service as integrations

    db_session.add(
        Integration(
            slug="phone",
            adapter="phone",
            name="phone",
            scopes=["phone:act"],
            status="active",
            config={"provider": "http"},
        )
    )
    await db_session.commit()

    async def opened_action(session, integration_id, action, args, *, actor):
        return SimpleNamespace(result={"opened": True, "data": {"opened": True}})

    monkeypatch.setattr(integrations, "execute_action", opened_action)
    ringing = await ev_tools.dispatch(
        db_session,
        "place_call",
        {"name": "Ned", "kind": "tel", "confirm": True},
        actor="master",
        allow_sensitive=True,
    )
    assert ringing.ok is True, ringing.error
    assert ringing.result is not None
    assert ringing.result["opened"] is True
    assert ringing.result["spoken"].startswith("Ringing ")

    async def closed_action(session, integration_id, action, args, *, actor):
        return SimpleNamespace(result={"opened": False, "error": "busy"})

    monkeypatch.setattr(integrations, "execute_action", closed_action)
    busy = await place_call(db_session, {"name": "Ned", "confirm": True}, actor="master")
    assert busy["ok"] is False
    assert "Ringing" not in busy["spoken"]
    assert busy["spoken"] == "busy"


async def test_place_call_exit_4_speaks_unavailable(
    db_session: AsyncSession, monkeypatch
) -> None:
    await _unlock(db_session)
    from app.integrations import service as integrations

    db_session.add(
        Integration(
            slug="phone",
            adapter="phone",
            name="phone",
            scopes=["phone:act"],
            status="active",
            config={"provider": "macos_life"},
        )
    )
    await db_session.commit()

    async def boom(session, integration_id, action, args, *, actor):
        raise LifeHelperError("no helper", exit_code=EXIT_NOT_AVAILABLE, error_code="not_available")

    monkeypatch.setattr(integrations, "execute_action", boom)
    result = await ev_tools.dispatch(
        db_session,
        "place_call",
        {"destination": "+1555", "confirm": True},
        actor="master",
        allow_sensitive=True,
    )
    assert result.ok is True or result.result is not None
    payload = result.result or {}
    assert payload.get("spoken") == "Calling isn't available on this device"


async def test_actuate_rejects_weapons_and_unknown_verbs(db_session: AsyncSession) -> None:
    await _unlock(db_session)
    killed = await ev_tools.dispatch(
        db_session, "actuate", {"verb": "instant_kill"}, actor="master", allow_sensitive=True
    )
    assert killed.ok is False
    assert killed.error == "refused"
    unknown = await ev_tools.dispatch(
        db_session, "actuate", {"verb": "launch_nukes"}, actor="master", allow_sensitive=True
    )
    payload = unknown.result or {}
    assert payload.get("ok") is False
    spoken = str(payload.get("spoken") or unknown.error or "")
    assert "volume.set" in spoken


async def test_locked_actuator_does_not_call_adapter(db_session: AsyncSession, monkeypatch) -> None:
    await ensure_seed_gates(db_session)
    await db_session.commit()
    called = {"n": 0}

    async def boom(*_a, **_k):
        called["n"] += 1
        raise AssertionError("adapter must not run")

    monkeypatch.setattr(home, "home_act", boom)
    locked = await ev_tools.dispatch(
        db_session,
        "home_act",
        {"entity": "front door", "action": "lock", "confirm": True},
        actor="master",
        allow_sensitive=True,
    )
    assert locked.ok is False
    assert locked.error == "training_wheels"
    assert called["n"] == 0


async def test_timer_survives_and_late_due_scan(db_session: AsyncSession) -> None:
    started = await ev_tools.dispatch(
        db_session,
        "start_timer",
        {"minutes": 37, "text": "37 minutes have passed"},
        actor="master",
    )
    assert started.ok is True, started.error
    row_id = UUID(started.result["id"])
    row = await db_session.get(OwnerTimer, row_id)
    assert row is not None
    row.fire_at = utcnow() - timedelta(minutes=1)
    await db_session.commit()

    from app.db import SessionLocal

    async with SessionLocal() as other:
        scanned = await timers.due_scan(other, daemon_was_down=True)
        await other.commit()
        assert scanned["fired"] == 1
        fired = await other.get(OwnerTimer, row_id)
        assert fired is not None
        assert fired.status == "fired"
        assert fired.late is True
        callouts = list(
            (await other.execute(select(Callout).where(Callout.source == "15"))).scalars().all()
        )
        assert callouts
        assert "this is late" in callouts[0].text


async def test_session_elapsed_from_voice_session(db_session: AsyncSession) -> None:
    started = utcnow() - timedelta(minutes=12)
    db_session.add(
        VoiceSession(device_id="mac", state="awake", created_at=started, updated_at=started)
    )
    await db_session.commit()
    elapsed = await ev_tools.dispatch(db_session, "session_elapsed", {}, actor="master")
    assert elapsed.ok is True
    assert elapsed.result["minutes"] >= 11
    assert "passed" in elapsed.result["spoken"]


async def test_maps_local_estimate_and_leave_by() -> None:
    eta = travel.maps_eta(origin="home", destination="Airport", provider="local")
    assert eta["estimate"] is True
    assert eta["minutes"] == 30
    assert "estimate" in (eta.get("honesty") or "").lower()
    start = utcnow() + timedelta(hours=2)
    leave = travel.leave_by_iso(start, eta["minutes"], 5)
    assert leave is not None
    expected = start - timedelta(minutes=35)
    from dateutil import parser as date_parser

    assert abs((date_parser.parse(leave) - expected).total_seconds()) < 2


async def test_indoor_route_empty_graph(db_session: AsyncSession) -> None:
    result = await ev_tools.dispatch(
        db_session, "indoor_route", {"to_room": "printer"}, actor="master"
    )
    assert result.ok is True
    assert result.result["ok"] is False
    assert result.result["spoken"] == "I don't have an indoor map"


async def test_whereabouts_never_claims_live_share(db_session: AsyncSession) -> None:
    honest = await travel.whereabouts_honest(db_session, "Ned")
    assert honest["live_share"] is False
    assert honest["source_kind"] in {"memory", "none"}
    assert "live share" in honest["spoken"].lower()


async def test_ticket_buy_without_confirm_does_not_purchase(db_session: AsyncSession) -> None:
    result = await ev_tools.dispatch(
        db_session,
        "ticket_buy",
        {"query": "opera tickets"},
        actor="master",
        allow_sensitive=True,
    )
    assert result.ok is True or result.result is not None
    payload = result.result or {}
    assert payload.get("purchased") is False
    assert payload.get("error") == "confirm_and_payment_required"
    direct = await ticket_buy(db_session, query="opera", confirm=False, actor="master")
    assert direct["purchased"] is False


async def test_calendar_add_requires_write_scope_then_event_id(db_session: AsyncSession) -> None:
    missing = await ev_tools.dispatch(
        db_session,
        "calendar_add",
        {
            "title": "Dinner",
            "start": (utcnow() + timedelta(days=1)).isoformat(),
            "end": (utcnow() + timedelta(days=1, hours=1)).isoformat(),
            "confirm": True,
        },
        actor="master",
        allow_sensitive=True,
    )
    assert missing.result is not None
    assert missing.result["ok"] is False

    db_session.add(
        Integration(
            slug="calendar",
            adapter="calendar",
            name="calendar",
            scopes=["calendar:read", "calendar:act"],
            status="active",
            config={"provider": "local", "write": True},
        )
    )
    await db_session.commit()
    added = await ev_tools.dispatch(
        db_session,
        "calendar_add",
        {
            "title": "Dinner Friday 7",
            "start": (utcnow() + timedelta(days=1)).isoformat(),
            "end": (utcnow() + timedelta(days=1, hours=1)).isoformat(),
            "confirm": True,
        },
        actor="master",
        allow_sensitive=True,
    )
    assert added.result is not None
    assert added.result["ok"] is True
    assert added.result["event_id"]
    assert added.result["evidence"]["id"] == added.result["event_id"]


async def test_training_wheels_last_step_unlocks_only_software_fleet(
    db_session: AsyncSession,
) -> None:
    await ensure_seed_gates(db_session)
    for step in TRAINING_STEPS:
        await complete_step(db_session, step)
    await db_session.commit()
    rows = {
        row.key: row.status
        for row in (await db_session.execute(select(FeatureGate))).scalars().all()
    }
    assert rows["life.call"] == "enabled"
    assert rows["actuator.software"] == "enabled"
    assert rows["maker.queue"] == "enabled"
    assert rows["actuator.drone"] == "locked"
    assert rows["instant_kill"] == "refused"
    assert rows["telecom_wiretap"] == "refused"


async def test_instant_kill_stays_refused(db_session: AsyncSession) -> None:
    await ensure_seed_gates(db_session)
    await unlock_after_training(db_session)
    await db_session.commit()
    row = (
        await db_session.execute(select(FeatureGate).where(FeatureGate.key == "instant_kill"))
    ).scalar_one()
    assert row.status == "refused"


async def test_gear_modes_and_unknown_mode(db_session: AsyncSession) -> None:
    explained = await ev_tools.dispatch(
        db_session, "gear_explain", {"device": "workshop-printer"}, actor="master"
    )
    assert explained.result["modes"]
    refused = await ev_tools.dispatch(
        db_session,
        "gear_set_mode",
        {"device": "workshop-printer", "mode": "warp"},
        actor="master",
    )
    assert refused.result["ok"] is False
    assert "draft" in refused.result["spoken"].lower() or "valid" in refused.result["spoken"].lower()
    empty = await ev_tools.dispatch(
        db_session, "gear_explain", {"device": "mystery-box"}, actor="master"
    )
    assert empty.result["spoken"] == "that device has no modes yet."


async def test_empties_fingerprint_and_list(db_session: AsyncSession, monkeypatch) -> None:
    monkeypatch.setattr("app.config.settings.timezone", "UTC")
    monkeypatch.setattr("app.config.settings.quiet_hours_start", "23:59")
    monkeypatch.setattr("app.config.settings.quiet_hours_end", "00:00")
    project = MakerProject(name="frame", status="sourcing")
    db_session.add(project)
    await db_session.flush()
    db_session.add(
        BomItem(project_id=project.id, name="PETG", qty=1, reorder_at=3, unit="kg")
    )
    db_session.add(
        GearSnapshot(device_id="drone-1", battery_percent=8, details={"battery_threshold": 15})
    )
    await db_session.commit()
    first = await workshop.scan_empties(db_session, emit=True)
    await db_session.commit()
    assert first["emitted"] >= 1
    second = await workshop.scan_empties(db_session, emit=True)
    assert second["emitted"] == 0
    listed = await ev_tools.dispatch(db_session, "list_empties", {}, actor="master")
    assert listed.result["count"] >= 1


async def test_quiet_hours_suppress_empty_spoken(db_session: AsyncSession, monkeypatch) -> None:
    monkeypatch.setattr("app.config.settings.timezone", "UTC")
    monkeypatch.setattr("app.config.settings.quiet_hours_start", "00:00")
    monkeypatch.setattr("app.config.settings.quiet_hours_end", "23:59")
    project = MakerProject(name="frame", status="sourcing")
    db_session.add(project)
    await db_session.flush()
    db_session.add(BomItem(project_id=project.id, name="PETG", qty=0, reorder_at=1))
    await db_session.commit()
    await workshop.scan_empties(db_session, emit=True)
    await db_session.commit()
    row = (
        await db_session.execute(select(Callout).where(Callout.source == "22"))
    ).scalars().first()
    assert row is not None
    assert row.spoken is False


async def test_delegate_cannot_include_forbidden_scopes(db_session: AsyncSession) -> None:
    bad = await ev_tools.dispatch(
        db_session,
        "delegate_grant",
        {"name": "Ned", "scopes": ["life.call", "home.lock"]},
        actor="master",
        allow_sensitive=True,
    )
    assert bad.result["ok"] is False
    assert bad.result["error"] == "forbidden_scope"
    also = await grant(
        db_session, name="Ned", scopes=["drone", "panic"], actor="master"
    )
    assert also["ok"] is False


async def test_delegate_grant_calendar_and_scope_enforced(db_session: AsyncSession) -> None:
    device = Device(name="Ned Phone", capabilities=["attention"], token_hash="c" * 64)
    db_session.add(device)
    await db_session.commit()
    good = await ev_tools.dispatch(
        db_session,
        "delegate_grant",
        {"name": "Ned", "scopes": ["calendar:read"], "device_id": str(device.id)},
        actor="master",
        allow_sensitive=True,
    )
    assert good.result["ok"] is True
    denied = await ev_tools.dispatch(
        db_session,
        "home_status",
        {},
        actor=f"device:{device.name}",
        allow_sensitive=True,
        device_id=device.id,
    )
    assert denied.ok is False
    assert denied.error == "delegate_scope"


async def test_panic_revokes_device_token(client: AsyncClient) -> None:
    created = await client.post(
        "/v1/devices",
        json={"name": "Phone A", "capabilities": ["attention"]},
    )
    token = created.json()["token"]
    device_id = created.json()["device"]["id"]
    panicked = await client.post(f"/v1/devices/{device_id}/panic")
    assert panicked.status_code == 200, panicked.text
    assert panicked.json()["revoked"] is True
    from httpx import ASGITransport, AsyncClient as Raw

    from app.main import app

    async with Raw(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as device_client:
        refused = await device_client.get("/v1/runtime/status")
        assert refused.status_code in {401, 403}


async def test_lock_all_master_key(client: AsyncClient, db_session: AsyncSession) -> None:
    first = await client.post("/v1/devices", json={"name": "A", "capabilities": ["voice"]})
    second = await client.post("/v1/devices", json={"name": "B", "capabilities": ["voice"]})
    locked = await client.post("/v1/runtime/lock-all")
    assert locked.status_code == 200, locked.text
    assert locked.json()["count"] >= 2
    for device_id in (first.json()["device"]["id"], second.json()["device"]["id"]):
        row = await db_session.get(Device, UUID(device_id))
        assert row is not None
        assert row.revoked_at is not None


async def test_biometric_failure_blocks_place_call(db_session: AsyncSession) -> None:
    await _unlock(db_session)
    blocked = await ev_tools.dispatch(
        db_session,
        "place_call",
        {"name": "Ned", "confirm": True},
        actor="device:phone",
        allow_sensitive=True,
        reverify_token=None,
    )
    assert blocked.ok is False
    assert blocked.error == "biometric_required"
    flagged = await ev_tools.dispatch(
        db_session,
        "place_call",
        {"name": "Ned", "confirm": True},
        actor="device:phone",
        allow_sensitive=True,
        reverify_token="not-a-real-proof",
    )
    assert flagged.ok is False
    assert flagged.error == "biometric_required"


async def test_route_briefing_weather_note_from_existing_provider(
    db_session: AsyncSession, monkeypatch
) -> None:
    from app.ev import navigation

    async def fake_weather(query: str, limit: int = 1, **_kwargs):
        return [SimpleNamespace(title="Rain", snippet="rain tonight", url="https://example.test")]

    monkeypatch.setattr("app.search.live.weather_results", fake_weather)
    note = await travel.weather_note("Airport")
    assert note == "rain, add 5 min"
    briefing = await navigation.route_briefing(db_session)
    assert briefing.schema_version == "ev.hud.route.v1"


async def test_volume_software_after_training(db_session: AsyncSession) -> None:
    await _unlock(db_session)
    result = await ev_tools.dispatch(
        db_session,
        "actuate",
        {"verb": "volume.set", "args": {"direction": "down"}},
        actor="master",
    )
    assert result.ok is True, result.error
    assert result.result["ok"] is True
    assert "Volume" in result.result["spoken"]
