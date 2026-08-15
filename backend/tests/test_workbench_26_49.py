"""Workbench items 26–49: shipped dispatch, HTTP, calibrate, daemon, filter."""

from __future__ import annotations

import json
import struct
from datetime import timedelta
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.ev import tools
from app.ev.assistant import last_calibration_report
from app.ev.callouts import list_callouts
from app.ev.diagnostics import run_calibration
from app.ev.hud import validate_hud
from app.ev.workbench import (
    fused_sense_pass,
    handle_calibrate,
    last_diagnostics_payload,
    maybe_calibration_tick,
)
from app.filter.envelope import GroundingMaterial
from app.filter.output_filter import apply_safety, run_output_filter
from app.ev.interaction import build_strategy
from app.models import (
    Alert,
    Callout,
    Delegate,
    Entity,
    Integration,
    PublicFeed,
    WatchlistItem,
)
from app.services.runtime import daemon_tick
from app.utils.text import utcnow


def _tiny_stl() -> bytes:
    header = b"solid ev" + b"\x00" * 72
    count = struct.pack("<I", 1)
    tri = struct.pack(
        "<ffffffffffffH",
        0, 0, 1,
        0, 0, 0,
        10, 0, 0,
        0, 10, 5,
        0,
    )
    return header[:80] + count + tri


async def _attachment(db_session: AsyncSession, data: bytes, filename: str, content_type: str) -> str:
    from app.models import Attachment, Event
    from app.storage.object_store import get_object_store

    event = Event(
        source="test",
        event_type="note",
        content={"text": filename},
        sha256="a" * 64,
        occurred_at=utcnow(),
    )
    db_session.add(event)
    await db_session.flush()
    key = f"attachments/{uuid4()}-{filename}"
    await get_object_store().put(key, data)
    row = Attachment(
        event_id=event.id,
        filename=filename,
        content_type=content_type,
        size_bytes=len(data),
        storage_key=key,
        sha256="b" * 64,
    )
    db_session.add(row)
    await db_session.commit()
    return str(row.id)


async def _integration(db_session: AsyncSession, adapter: str, scopes: list[str], config: dict) -> Integration:
    row = Integration(
        slug=f"{adapter}-{uuid4().hex[:8]}",
        adapter=adapter,
        name=adapter,
        scopes=scopes,
        status="active",
        config=config,
    )
    db_session.add(row)
    await db_session.commit()
    return row


async def invoke(client: AsyncClient, name: str, arguments: dict | None = None, *, sensitive: bool = False):
    resp = await client.post(
        "/v1/gateway/tools",
        json={
            "name": name,
            "arguments": arguments or {},
            "allow_sensitive": sensitive,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("ok"), body
    return body.get("result") or {}


async def _wheels(db_session: AsyncSession) -> None:
    from app.ev.training_wheels import TRAINING_STEPS, complete_step

    for step in TRAINING_STEPS:
        await complete_step(db_session, step)
    await db_session.commit()


def _assert_card(payload: dict) -> None:
    schema, model = validate_hud(payload)
    assert schema == "ev.hud.card.v1"
    dumped = model.model_dump()
    assert dumped["title"]
    assert dumped["body"]


async def test_calibrate_dispatch_hud_and_last(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    ran = await client.post("/v1/diagnostics/calibrate")
    assert ran.status_code == 200, ran.text
    body = ran.json()
    assert body["overall"] in {"ok", "degraded", "failed"}
    assert body["checks"]
    names = {c["name"] for c in body["checks"]}
    assert "database" in names

    last = await client.get("/v1/diagnostics/last")
    assert last.status_code == 200, last.text
    payload = last.json()
    assert payload["overall"] == body["overall"]
    last_names = {c["name"] for c in (payload["report"] or {}).get("checks") or []}
    assert last_names == names
    assert payload["hud"]["schema_version"] == "ev.hud.card.v1"

    # After HTTP calibrate writes a HudPush, /v1/hud/card must return that
    # card — not 500 on naive SQLite timestamps, not a generic status_card.
    hud = await client.get("/v1/hud/card")
    assert hud.status_code == 200, hud.text
    card = hud.json()
    assert card["schema_version"] == "ev.hud.card.v1"
    assert card["title"] == "Diagnostics"
    card_blob = (card.get("body") or "") + json.dumps(card.get("meta") or {})
    assert "database" in card_blob

    dispatched = await tools.dispatch(db_session, "calibrate", {}, actor="master")
    assert dispatched.ok, dispatched.error
    result = dispatched.result or {}
    assert result["spoken"]
    _assert_card(result["hud"])
    rows = (result["hud"].get("meta") or {}).get("rows") or []
    assert any(r.get("name") == "database" for r in rows)
    worst = next((c for c in result["checks"] if c["status"] == "failed"), None)
    if worst:
        assert worst["name"] in result["spoken"]
    else:
        assert "ok" in result["spoken"].lower() or "calibration" in result["spoken"].lower()
    await db_session.commit()
    after = await invoke(client, "calibrate", {})
    pushed = await client.get("/v1/hud/card")
    assert pushed.status_code == 200, pushed.text
    pushed_card = pushed.json()
    assert pushed_card["title"] == after["hud"]["title"]
    assert pushed_card["body"] == after["hud"]["body"]


async def test_daemon_failed_fingerprint_and_quiet_hours(
    db_session: AsyncSession, monkeypatch
) -> None:
    await _integration(
        db_session,
        "octoprint",
        ["printer:read", "printer:act"],
        {"provider": "local", "connected": False},
    )
    first = await maybe_calibration_tick(db_session)
    await db_session.commit()
    assert first["callouts"] == 1
    assert first["fingerprints"] == ["diagnostics:octoprint.ping:failed"]
    rows = await list_callouts(db_session, limit=20)
    failed = [r for r in rows if r.source_item == "diagnostics:octoprint.ping:failed"]
    assert len(failed) == 1

    second = await daemon_tick(db_session)
    await db_session.commit()
    rows = await list_callouts(db_session, limit=50)
    failed = [r for r in rows if r.source_item == "diagnostics:octoprint.ping:failed"]
    assert len(failed) == 1
    assert second["calibration_tick"]["callouts"] == 0

    third = await daemon_tick(db_session)
    await db_session.commit()
    rows = await list_callouts(db_session, limit=50)
    failed = [r for r in rows if r.source_item == "diagnostics:octoprint.ping:failed"]
    assert len(failed) == 1
    assert third["calibration_tick"]["callouts"] == 0

    monkeypatch.setattr(settings, "quiet_hours_start", "00:00")
    monkeypatch.setattr(settings, "quiet_hours_end", "23:59")
    await _integration(
        db_session,
        "octoprint",
        ["printer:read", "printer:act"],
        {"provider": "local", "connected": False},
    )
    # Same fingerprint already exists; flip another adapter name via a second failed check
    # is not needed — quiet hours still persist the prior row as unspoken on a new fp.
    from app.ev.callouts import emit_callout

    row = await emit_callout(
        db_session,
        "radio.rssi failed.",
        source="diagnostics",
        source_item="diagnostics:radio.rssi:failed",
        hud={"schema_version": "ev.hud.card.v1", "title": "Diagnostics", "body": "radio.rssi failed.", "generated_at": utcnow().isoformat()},
    )
    await db_session.commit()
    assert row.spoken is False


async def test_research_citations_and_filter_allowlist(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "search_provider", "mock")
    dispatched = await tools.dispatch(
        db_session, "research", {"question": "graphene oxide conductivity"}, actor="master"
    )
    assert dispatched.ok, dispatched.error
    result = dispatched.result or {}
    assert result["citations"]
    urls = [c["url"] for c in result["citations"] if c.get("url")]
    assert urls
    spoken = result["spoken"]
    assert "according to" in spoken.lower()
    _assert_card(result["hud"])
    await db_session.commit()
    via_http = await invoke(
        client, "research", {"question": "graphene oxide conductivity"}
    )
    pushed = await client.get("/v1/hud/card")
    assert pushed.status_code == 200, pushed.text
    assert pushed.json()["title"] == via_http["hud"]["title"]
    assert pushed.json()["body"] == via_http["hud"]["body"]
    assert pushed.json()["title"] == "Research"
    filtered, _safety, _flags = apply_safety(spoken + " " + " ".join(urls))
    for url in urls:
        assert url in filtered

    monkeypatch.setattr(settings, "search_provider", "none")
    memory_only = await tools.dispatch(
        db_session, "research", {"question": "graphene oxide conductivity"}, actor="master"
    )
    assert memory_only.ok, memory_only.error
    payload = memory_only.result or {}
    assert payload.get("answer")
    assert payload.get("memory_only") is True
    assert "memory-only" in payload["spoken"].lower() or "disabled" in payload["spoken"].lower()


async def test_weather_brief_and_public_lookup(
    db_session: AsyncSession, monkeypatch
) -> None:
    from app.search import live
    from app.search.providers import SearchResult

    async def fake_weather(query: str, *, limit: int = 3, lat=None, lon=None):
        return [
            SearchResult(
                title="Weather in home",
                url="https://open-meteo.com/",
                snippet="home: clear sky. 22°C",
            )
        ]

    monkeypatch.setattr(live, "weather_results", fake_weather)
    weather = await tools.dispatch(db_session, "get_weather", {"query": "what's the weather"}, actor="master")
    assert weather.ok, weather.error
    assert weather.result["spoken"]
    assert "22" in weather.result["spoken"] or "clear" in weather.result["spoken"]
    _assert_card(weather.result["hud"])

    brief = await tools.dispatch(db_session, "brief_me", {"topic": "today"}, actor="master")
    assert brief.ok, brief.error
    assert brief.result.get("objective") is not None
    assert brief.result.get("spoken")

    denied = await tools.dispatch(
        db_session, "brief_share", {"delegate": "Ned"}, actor="master", allow_sensitive=True
    )
    assert denied.ok, denied.error
    assert denied.result.get("denied") is True
    await db_session.commit()

    db_session.add(
        Delegate(
            person_name="Ned",
            scopes=["briefing:read"],
            not_after=utcnow() + timedelta(days=1),
        )
    )
    await db_session.commit()
    shared = await tools.dispatch(
        db_session, "brief_share", {"delegate": "Ned"}, actor="master", allow_sensitive=True
    )
    assert shared.ok and shared.result.get("ok") is True

    pub = await tools.dispatch(
        db_session, "public_lookup", {"query": "Acme Robotics Inc", "kind": "org"}, actor="master"
    )
    assert pub.ok, pub.error
    urls = [c["url"] for c in pub.result.get("citations") or []]
    assert urls
    assert any("wikipedia.org" in u or "sec.gov" in u for u in urls)

    refused = await tools.dispatch(
        db_session,
        "public_lookup",
        {"query": "Jane Doe home address phone", "kind": "law"},
        actor="master",
    )
    assert refused.ok, refused.error
    assert refused.result.get("refused") == "private_person_pii"


async def test_gear_health_sense_and_head_injury(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    from app.api.runtime import RuntimeHeartbeatCreate  # noqa: F401
    from app.models import Device

    device = Device(name="iphone", device_type="phone")
    db_session.add(device)
    await db_session.commit()
    await db_session.commit()
    beat = await client.post(
        "/v1/runtime/heartbeat",
        json={
            "device_id": str(device.id),
            "status": "ok",
            "listener_state": "listening",
            "battery_percent": 17,
            "storage_free_b": 8_000_000_000,
        },
    )
    assert beat.status_code == 201, beat.text
    power = await invoke(client, "gear_power", {"device": "iphone"})
    assert power.get("battery_percent") == 17
    assert "17" in power["spoken"]
    alerts = await client.get("/v1/alerts")
    gear_alerts = [a for a in alerts.json() if a.get("kind") == "gear"]
    assert len(gear_alerts) <= 2

    snap = await client.post(
        "/v1/health/snapshot",
        json={"source": "test", "metrics": {"sleep_hours": 5.0, "hrv_ms": 40, "resting_hr": 62, "steps": 2000}},
    )
    assert snap.status_code == 201, snap.text
    look = await invoke(client, "health_how_do_i_look", {}, sensitive=True)
    assert look.get("readiness") == snap.json()["readiness"]
    assert look.get("band") == snap.json()["band"]

    screen = await invoke(client, "head_injury_screen", {}, sensitive=True)
    assert "I'm not a doctor. Get medical care if" in screen["disclaimer"]
    assert "I'm not a doctor. Get medical care if" in screen["spoken"]
    assert screen.get("diagnosis") is None
    blob = " ".join(screen.get("questions") or [])
    assert "lose consciousness" in blob.lower()
    assert "concussion yes" not in screen["spoken"].lower()

    strategy = build_strategy("I hit my head")
    report = await run_output_filter(
        "You have a concussion. Sit down. https://en.wikipedia.org/wiki/Concussion",
        strategy=strategy,
        grounding=[],
    )
    assert "you have a concussion" not in report.final_text.lower()
    assert "https://en.wikipedia.org/wiki/Concussion" in report.final_text

    await client.post(
        "/v1/events",
        json={"source": "test", "event_type": "note", "text": "I feel so lonely and isolated tonight"},
    )
    db_session.add(
        WatchlistItem(kind="deadline", value="contract renewal", priority=0.9, active=True)
    )
    await db_session.commit()
    fused = await fused_sense_pass(db_session)
    await db_session.commit()
    assert fused.get("candidates", 1) <= 1
    why = await invoke(client, "why_did_you_ping")
    if fused.get("callout"):
        assert why.get("why_now")
        pending = await client.get("/v1/alerts?status=pending")
        if pending.json():
            alert_id = pending.json()[0]["id"]
            gone = await client.post(f"/v1/alerts/{alert_id}/dismiss")
            assert gone.status_code == 200

    monkeypatch.setattr(settings, "quiet_hours_start", "00:00")
    monkeypatch.setattr(settings, "quiet_hours_end", "23:59")
    denied = await fused_sense_pass(db_session)
    await db_session.commit()
    assert denied.get("callout") is None or denied.get("policy")


async def test_print_estimate_telemetry_camera_drone_beacon(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    queued = await invoke(client, "print_start", {"project": "spacer"}, sensitive=True)
    assert queued.get("queued") is True
    assert "no printer connected" in queued["spoken"].lower()

    await _wheels(db_session)
    await _integration(
        db_session,
        "octoprint",
        ["printer:read", "printer:act"],
        {"provider": "local", "job_status": "done"},
    )
    locked = await invoke(
        client, "print_start", {"project": "spacer", "confirm": False}, sensitive=True
    )
    assert locked.get("needs_confirm") is True

    started = await invoke(
        client, "print_start", {"project": "spacer", "confirm": True}, sensitive=True
    )
    assert started.get("started") is True
    from app.ev.hardware import poll_print_jobs

    await poll_print_jobs(db_session)
    await db_session.commit()
    listed = await client.get("/v1/assistant/callouts?limit=20")
    assert listed.status_code == 200
    prints = [c for c in listed.json() if c.get("source") == "print"]
    assert prints

    aid = await _attachment(db_session, _tiny_stl(), "spacer.stl", "model/stl")
    est = await invoke(client, "estimate_print", {"attachment_id": aid})
    assert est.get("estimated_minutes") or est.get("estimate") == "owner_or_heuristic"
    assert est.get("estimate") in {"slicer", "owner_or_heuristic", None} or est.get("estimated_minutes")

    sess = await client.post("/v1/telemetry/sessions", json={"label": "flight"})
    assert sess.status_code == 201, sess.text
    sample = await client.post(
        "/v1/telemetry/sample",
        json={
            "source": "phone",
            "battery": 64,
            "alt": 12,
            "speed": 3,
            "lat": 37.7,
            "lon": -122.4,
        },
    )
    assert sample.status_code == 201, sample.text
    batt = await invoke(client, "gear_power", {"device": "phone"})
    assert batt.get("battery_percent") == 64
    assert "phone" in batt["spoken"].lower() or "stand-in" in batt["spoken"].lower()

    cam = await invoke(client, "camera_replay", {"camera": "lab", "at": "16:00"}, sensitive=True)
    assert cam.get("discovered_lan") is False
    assert "configured" in cam["spoken"].lower() or cam.get("configured") is False

    await _integration(db_session, "drone", ["drone:act"], {"provider": "local", "configured": True})
    no_confirm = await invoke(client, "drone", {"command": "takeoff"}, sensitive=True)
    assert no_confirm.get("needs_confirm") is True
    weapons = await invoke(client, "drone", {"command": "fire missile"}, sensitive=True)
    assert weapons.get("refused") == "weapons"
    for cmd in ("takeoff", "hover", "land", "rtl"):
        out = await invoke(client, "drone", {"command": cmd, "confirm": True}, sensitive=True)
        assert out.get("sim") is True
        assert out.get("audited") is True

    db_session.add(Entity(entity_type="person", name="Riley", canonical_key="person:riley"))
    await db_session.commit()
    refuse = await invoke(client, "find_gear", {"label": "Riley"})
    assert refuse.get("refused") == "person_without_beacon"
    await client.post(
        "/v1/beacons",
        json={"label": "backpack tag", "kind": "ble", "last_lat": 37.7, "last_lon": -122.4},
    )
    found = await invoke(client, "find_gear", {"label": "backpack tag"})
    assert found.get("last_lat") == 37.7


async def test_where_watchlist_media_voice_structure_plate(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    unknown = await invoke(client, "where_is", {"name": "Stranger X"})
    assert unknown.get("refused") == "unknown_person"

    db_session.add(Entity(entity_type="person", name="May", canonical_key="person:may"))
    await db_session.commit()
    memory = await invoke(client, "where_is", {"name": "May"})
    assert "memory only" in memory["spoken"].lower()

    share = await client.post(
        "/v1/location-shares",
        json={"name": "May", "last_lat": 40.7, "last_lon": -74.0},
    )
    assert share.status_code == 201, share.text
    live = await invoke(client, "where_is", {"name": "May"})
    assert live.get("live") is True
    assert live.get("lat") == 40.7

    added = await invoke(client, "watchlist_add", {"value": "NWS county alert", "kind": "topic"})
    assert added.get("value")
    db_session.add(
        PublicFeed(
            kind="nws",
            url="https://example.invalid/nws.rss",
            label="NWS",
            last_items=[{"title": "NWS county alert", "summary": "Watch this"}],
        )
    )
    await db_session.commit()
    from app.ev.workbench import poll_public_feeds

    poll = await poll_public_feeds(db_session)
    await db_session.commit()
    assert poll["created"] >= 1
    digest = await invoke(client, "alerts_digest")
    assert digest.get("count", 0) >= 1

    clip = await _attachment(db_session, b"ftypisom ffmpeg lavf synthetic", "clip.mp4", "video/mp4")
    media = await invoke(client, "media_check", {"attachment_id": clip})
    assert media["label"] in {"likely_edited", "no_known_artifacts", "inconclusive"}
    assert "this is not proof" in media["spoken"].lower()
    assert "this is real" not in media["spoken"].lower()

    banned = await invoke(client, "set_voice", {"voice_id": "interrogation"})
    assert banned.get("refused") == "interrogation"
    voice = await invoke(client, "set_voice", {"voice_id": "lower"})
    from app.voice.contracts import SpeechStyle
    from app.voice.tts import MetaSynthesizer

    spoken = await MetaSynthesizer().synthesize("hello", style=SpeechStyle())
    assert spoken.details.get("voice") == voice["voice_id"]
    assert spoken.details.get("rate") == voice["tts_rate"]
    reset = await invoke(client, "set_voice", {"voice_id": "reset"})
    assert reset["voice_id"]

    photo = await _attachment(db_session, _tiny_stl(), "shelf.jpg", "image/jpeg")
    structure = await invoke(
        client, "estimate_structure", {"attachment_id": photo, "reference_length": 20}
    )
    assert "low confidence" in structure["spoken"].lower()
    assert "x-ray" not in structure["spoken"].lower()
    assert "98%" not in structure["spoken"]

    plate = await invoke(client, "whats_on_my_plate")
    assert plate.get("calendar_only") is True
    draft = await invoke(client, "draft_reply", {"mail_id": "m1", "body": "Thanks"}, sensitive=True)
    assert draft.get("sent") is False
    send = await invoke(client, "draft_reply", {"mail_id": "m1", "send": True}, sensitive=True)
    assert send.get("sent") is not True

    first = await client.post("/v1/lookout/utterance", json={"text": "hello from watch"})
    assert first.status_code == 200, first.text
    cid = first.json()["conversation_id"]
    second = await client.post(
        "/v1/lookout/utterance",
        json={"text": "and from the desk", "conversation_id": cid},
    )
    assert second.status_code == 200, second.text
    transcript = await client.get("/v1/lookout/transcript", params={"conversation_id": cid})
    assert transcript.status_code == 200
    turns = transcript.json()["turns"]
    texts = " ".join(t.get("text") or "" for t in turns)
    assert "hello from watch" in texts
    assert "and from the desk" in texts
    live = await client.get("/v1/lookout/live", params={"conversation_id": cid})
    assert live.status_code == 200
    assert "hello from watch" in live.text
    utterance_card = await client.get("/v1/hud/card")
    assert utterance_card.status_code == 200, utterance_card.text
    assert utterance_card.json()["schema_version"] == "ev.hud.card.v1"
    assert utterance_card.json()["body"]
    assert utterance_card.json()["title"] != "EV status"

    names = {spec["name"] for spec in tools.list_tools()}
    required = {
        "calibrate",
        "research",
        "print_start",
        "estimate_print",
        "gear_power",
        "health_how_do_i_look",
        "head_injury_screen",
        "brief_me",
        "brief_share",
        "where_is",
        "camera_replay",
        "watchlist_add",
        "alerts_digest",
        "media_check",
        "set_voice",
        "public_lookup",
        "find_gear",
        "estimate_structure",
        "why_did_you_ping",
        "whats_on_my_plate",
        "draft_reply",
    }
    assert required <= names
    assert not any("glasses" in spec["name"] for spec in tools.list_tools())


async def test_hud_card_survives_naive_sqlite_timestamp(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """SQLite returns naive created_at; last_hud_payload must not TypeError."""

    from datetime import datetime

    from app.models import HudPush

    payload = {
        "schema_version": "ev.hud.card.v1",
        "generated_at": utcnow().isoformat(),
        "title": "Diagnostics",
        "body": "database is ok",
        "priority": 0.3,
        "meta": {"rows": [{"name": "database", "status": "ok"}]},
    }
    db_session.add(
        HudPush(
            schema_version="ev.hud.card.v1",
            payload=payload,
            source="calibrate",
            prefer_haptic=True,
            created_at=datetime.now().replace(tzinfo=None),
        )
    )
    await db_session.commit()
    from app.ev.workbench import last_hud_payload

    loaded = await last_hud_payload(db_session)
    assert loaded is not None
    assert loaded["title"] == "Diagnostics"
    card = await client.get("/v1/hud/card")
    assert card.status_code == 200, card.text
    assert card.json()["title"] == "Diagnostics"
    assert "database" in (card.json().get("body") or "")


async def test_asgi_calibrate_last_twice(client: AsyncClient) -> None:
    first = await client.post("/v1/diagnostics/calibrate")
    last = await client.get("/v1/diagnostics/last")
    assert first.status_code == 200 and last.status_code == 200
    assert last.json()["overall"] == first.json()["overall"]
    second = await client.post("/v1/diagnostics/calibrate")
    last2 = await client.get("/v1/diagnostics/last")
    assert second.status_code == 200 and last2.status_code == 200
    assert last2.json()["report"]["checks"]
    card = await client.get("/v1/hud/card")
    assert card.status_code == 200, card.text
    assert card.json()["title"] == "Diagnostics"
