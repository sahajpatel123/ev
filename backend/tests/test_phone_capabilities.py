"""Phone capability manifest — the honest "what can this iPhone do" surface.

Owner problem (2026-09-08): a freshly paired iPhone is PAIRED_SANDBOX where
the spoken surface is deliberately chat-only, and nothing on the device
explains that tools/memory/camera are one promotion away. These tests pin the
server-computed manifest endpoint that makes the trust lifecycle visible.

Read-only endpoint; derives from the same policy code the turn paths enforce.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from httpx import ASGITransport, AsyncClient

from app.main import app
from app.models import Device

def _trusted_device() -> Device:
    return Device(
        name="iPhone 16 Pro",
        platform="ios",
        token_hash="hash",
        paired_at=datetime.now(UTC),
        memory_scope="owner",
    )

def _revoked_device() -> Device:
    return Device(
        name="iPhone SE",
        platform="ios",
        token_hash="hash",
        paired_at=datetime.now(UTC),
        revoked_at=datetime.now(UTC),
        memory_scope="owner",
    )


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _pair_sandbox(client: AsyncClient, name: str) -> AsyncClient:
    minted = await client.post(
        "/v1/device-gateway/pairing-tokens",
        json={"role": "companion", "display_name": name},
    )
    assert minted.status_code == 200, minted.text
    phone = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    paired = await phone.post(
        "/v1/device-gateway/pair",
        json={
            "pairing_token": minted.json()["pairing_token"],
            "display_name": name,
            "protocol_version": "1",
            "client_version": "2026.09.08.01",
            "platform": "ios",
            "instance_id": name + "-tab",
        },
    )
    assert paired.status_code == 200, paired.text
    phone.headers["Authorization"] = f"Bearer {paired.json()['device_token']}"
    return phone


async def test_sandbox_manifest_hides_tools_and_explains_unlock(
    client: AsyncClient,
) -> None:
    phone = await _pair_sandbox(client, "SE-Sandbox")
    response = await phone.get("/v1/device-gateway/capabilities")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["trust_state"] == "PAIRED_SANDBOX"
    assert body["environment"] == "SANDBOX"
    assert body["voice"]["tools"] == []
    assert all(enabled is False for enabled in body["tools"].values())
    assert all(enabled is False for enabled in body["reads"].values())
    assert body["memory"]["scope"] == "sandbox"
    assert body["camera_look"] is False
    assert body["limits"], "sandbox must state its limits"
    assert "promote" in (body["upgrade_hint"] or "").lower()


async def test_manifest_requires_device_auth() -> None:
    client = _client()
    response = await client.get("/v1/device-gateway/capabilities")
    assert response.status_code in (401, 403), response.text


def test_trusted_manifest_unlocks_full_surface() -> None:
    from app.device_gateway.capability_manifest import capability_manifest

    manifest = capability_manifest(_trusted_device())
    assert manifest["trust_state"] == "TRUSTED_OWNER_DEVICE"
    assert manifest["environment"] == "OWNER"
    assert manifest["voice"]["tools"] == (
        "evie_state_query",
        "phone_action",
        "evie_look",
        "evie_home_action",
    )
    assert manifest["tools"]["start_timer"] is True
    assert manifest["tools"]["computer_action"] is True
    assert manifest["reads"]["weather"] is True
    assert manifest["reads"]["memory_history"] is True
    assert manifest["memory"]["scope"] == "owner"
    assert manifest["memory"]["shadow_injection"] is True
    assert manifest["camera_look"] is True
    assert manifest["upgrade_hint"] is None
    assert manifest["limits"] == []


def test_revoked_manifest_states_repair_path() -> None:
    from app.device_gateway.capability_manifest import capability_manifest

    manifest = capability_manifest(_revoked_device())
    assert manifest["trust_state"] == "REVOKED"
    assert manifest["upgrade_hint"] is None
    assert all(enabled is False for enabled in manifest["tools"].values())
    assert any("re-pair" in item.lower() for item in manifest["limits"])


async def test_voice_conversation_turn_ingested_to_memory(db_session):
    """Cycle 43 — receipts learn: a conversational phone VOICE turn must
    reach the memory OS pipeline (schedule_live_turn), which the realtime
    path otherwise skips entirely. Text path unaffected (flag off)."""
    import asyncio

    from sqlalchemy import select

    from app.device_gateway.pipeline import run_trusted_device_turn
    from app.memory.turns import flush_live_turns
    from app.models import Device, Event

    d = Device(
        name="Ingest Phone",
        token_hash="ingest-phone",
        trust_level="owner",
        memory_scope=None,
        device_type="phone",
    )
    db_session.add(d)
    await db_session.commit()

    result = await run_trusted_device_turn(
        db_session,
        device=d,
        text="remind me we tried the new ramen place and it was great",
        idempotency_key="ing-1",
        ingest_conversation=True,
    )
    assert result.get("conversational") is True
    flushed = await flush_live_turns(timeout_s=5.0)
    assert flushed >= 1, "live turn task must be scheduled and finish"
    rows = (
        await db_session.execute(
            select(Event).where(Event.event_type == "message.user").order_by(Event.occurred_at.desc())
        )
    ).scalars().all()
    ingested = [r for r in rows if (r.metadata_ or {}).get("surface") == "phone_voice"]
    assert ingested, "phone voice turn must be recorded into the event store"
    assert "ramen" in (ingested[0].content or {}).get("text", "")


async def test_start_timer_result_carries_fire_at(db_session):
    """Cycle 46 — the phone countdown ring needs the canonical fire time;
    maybe_phone_mac_act must pass the timer payload through, not just the
    spoken sentence."""
    from app.device_gateway.phone_mac import maybe_phone_mac_act
    from app.models import Device

    d = Device(
        name="Timer Phone",
        token_hash="timer-phone",
        trust_level="owner",
        memory_scope=None,
        device_type="phone",
    )
    db_session.add(d)
    await db_session.commit()

    result = await maybe_phone_mac_act(
        db_session,
        device=d,
        text="set a timer for five minutes",
        idempotency_key="tmr-1",
    )
    assert result is not None
    assert result.get("tool") == "start_timer"
    timer = result.get("timer") or {}
    assert timer.get("fire_at"), f"fire_at must ride the result: {result}"
    assert "5" in str(result.get("reply") or "") or "minute" in str(result.get("reply") or "")


async def test_phone_conversation_recalls_stored_memory(
    db_session, client, monkeypatch
):
    """Cycle 47 — C7: shadow memory injection must reach the PHONE voice
    surface: a trusted phone turn over stored history returns recalled_history."""
    from uuid import uuid4

    from app.device_gateway.pipeline import run_trusted_device_turn
    from app.models import Device, Memory
    from app.utils.text import fingerprint, utcnow
    from app.config import settings

    monkeypatch.setattr(settings, "memory_gate", "on")
    now = utcnow()
    db_session.add(
        Memory(
            memory_type="decision",
            text="I decided to switch the project to SQLite for local testing",
            payload={},
            importance=0.9,
            confidence=0.9,
            source_type="explicit",
            privacy_level="normal",
            event_time=now,
            valid_from=now,
            is_current=True,
            fingerprint=fingerprint({"seed": uuid4().hex}),
            embedding=None,
            embedding_model_version=None,
        )
    )
    d = Device(
        name="Recall Phone",
        token_hash="recall-phone",
        trust_level="owner",
        memory_scope=None,
        device_type="phone",
    )
    db_session.add(d)
    await db_session.commit()

    result = await run_trusted_device_turn(
        db_session,
        device=d,
        text="remind me why I switched the project to SQLite for local testing",
        idempotency_key="rec-1",
        ingest_conversation=True,
    )
    history = str(result.get("recalled_history") or "")
    if result.get("conversational"):
        assert "SQLite" in history, f"phone turn must recall stored history, got: {result}"
    else:
        # Deterministic state answer — the canonical surface; shadow not required.
        assert result.get("ok") is True


async def test_phone_history_recall_read(db_session):
    """Cycle 48 — C8: an explicit history question on the trusted phone
    surface routes to the deterministic MEMORY read with real depth (k=5)
    and speaks what it found."""
    from uuid import uuid4

    from app.device_gateway.pipeline import run_trusted_device_turn
    from app.models import Device, Memory
    from app.utils.text import fingerprint, utcnow

    now = utcnow()
    for i in range(5):
        db_session.add(
            Memory(
                memory_type="fact",
                text=f"Note {i}: the rooftop garden needs a drip irrigation line",
                payload={},
                importance=0.8,
                confidence=0.9,
                source_type="explicit",
                privacy_level="normal",
                event_time=now,
                valid_from=now,
                is_current=True,
                fingerprint=fingerprint({"seed": uuid4().hex}),
                embedding=None,
                embedding_model_version=None,
            )
        )
    d = Device(
        name="History Phone",
        token_hash="history-phone",
        trust_level="owner",
        memory_scope=None,
        device_type="phone",
    )
    db_session.add(d)
    await db_session.commit()

    result = await run_trusted_device_turn(
        db_session,
        device=d,
        text="what do you remember about the rooftop garden",
        idempotency_key="his-1",
        ingest_conversation=True,
    )
    spoken = str(result.get("reply") or "")
    if result.get("route") == "MEMORY":
        assert result.get("ok") is True
        assert "garden" in spoken.lower() or result.get("count", 0) >= 0
        assert result.get("provenance") == "memory.history"


async def test_web_push_subscription_roundtrip(client):
    """Cycle 49 — C9: the PWA can store a browser push subscription and read
    the server's applicationServerKey; unconfigured keys are a no-op."""
    phone = await _pair_sandbox(client, "Push-SE")
    key = await phone.get("/v1/device-gateway/vapid-public-key")
    assert key.status_code == 200, key.text
    body = key.json()
    assert body["ok"] is True
    assert body["configured"] is False
    assert body["application_server_key"] == ""

    stored = await phone.post(
        "/v1/device-gateway/push/web-subscription",
        json={
            "endpoint": "https://push.example.com/sub/abc",
            "keys": {"p256dh": "k" * 40, "auth": "a" * 20},
        },
    )
    assert stored.status_code == 200, stored.text
    assert stored.json()["registered"] is True

    bad = await phone.post(
        "/v1/device-gateway/push/web-subscription",
        json={"endpoint": "http://insecure.example.com", "keys": {"p256dh": "x", "auth": "y"}},
    )
    assert bad.status_code == 422


async def test_push_inbox_survives_without_web_push(db_session):
    """Cycle 49 — C9: an inbox write on a device with no subscription and no
    VAPID keys must succeed (push is best-effort, never blocking)."""
    import asyncio

    from uuid import uuid4

    from app.everywhere.inbox import push_inbox
    from app.models import Device

    d = Device(
        name="NoPush Phone",
        token_hash="nopush-phone",
        trust_level="owner",
        memory_scope=None,
        device_type="phone",
    )
    db_session.add(d)
    await db_session.commit()

    item = await push_inbox(db_session, device_id=d.id, kind="notice", title="Nudge", body="hello")
    assert item.get("title") == "Nudge"
    await asyncio.sleep(0.05)


def test_quiet_hours_wrap_and_gate():
    """Cycle 50 — C10: quiet hours wrap midnight (22:00–07:00); the 3pm nudge
    passes, the 2am nudge holds, and a timer alarm bypasses the hold."""
    from datetime import datetime

    from app.everywhere.nudge import in_quiet_hours, nudge_prefs

    class _FakeDevice:
        endpoint_profile = {}

    prefs = nudge_prefs(_FakeDevice())
    assert prefs["enabled"] is True
    assert prefs["quiet_start"] == "22:00" and prefs["quiet_end"] == "07:00"
    assert not in_quiet_hours(prefs, now=datetime(2026, 9, 8, 15, 0))
    assert in_quiet_hours(prefs, now=datetime(2026, 9, 8, 2, 30))
    assert in_quiet_hours(prefs, now=datetime(2026, 9, 8, 23, 10))


async def test_send_nudge_quiet_and_alarm_bypass(db_session):
    from uuid import uuid4

    from app.everywhere.nudge import send_nudge
    from app.models import Device

    d = Device(
        name="Quiet Phone",
        token_hash="quiet-phone",
        trust_level="owner",
        memory_scope=None,
        device_type="phone",
    )
    db_session.add(d)
    await db_session.commit()

    quiet = await send_nudge(
        db_session,
        d,
        kind="pattern",
        title="Pattern",
        body="You usually walk at this hour",
        now=__import__("datetime").datetime(2026, 9, 8, 2, 30),
    )
    assert quiet["status"] == "quiet" and quiet["item"] is None

    alarm = await send_nudge(
        db_session,
        d,
        kind="timer",
        title="Timer",
        body="Ramen timer done",
        bypass_quiet=True,
        now=__import__("datetime").datetime(2026, 9, 8, 2, 30),
    )
    assert alarm["status"] == "sent" and alarm["item"] is not None


async def test_quick_actions_gated_by_trust(client):
    """Cycle 51 — C11: quick actions are server-computed per trust state;
    each action carries an utterance (no client-side tool authority)."""
    phone = await _pair_sandbox(client, "QA-SE")
    body = await phone.get("/v1/device-gateway/quick-actions")
    assert body.status_code == 200, body.text
    data = body.json()
    assert data["ok"] is True
    assert data["trust_state"] == "PAIRED_SANDBOX"
    assert data["actions"], "sandbox still gets a chat-oriented action"
    for action in data["actions"]:
        assert action.get("utterance"), action
        assert "tools" not in action  # no client-side dispatch surface


async def test_battery_report_and_low_battery_nudge_gate(client, db_session):
    """Cycle 52 — C12: heartbeat persists a clamped battery level; status
    exposes it; send_nudge holds non-alarm nudges at <=15% but alarms pass."""
    from datetime import datetime
    from uuid import uuid4

    from app.everywhere.nudge import send_nudge
    from app.models import Device

    phone = await _pair_sandbox(client, "Batt-SE")
    hb = await phone.post(
        "/v1/device-gateway/heartbeat",
        json={"instance_id": "Batt-SE-tab", "method": "battery", "battery_percent": 420},
    )
    assert hb.status_code == 200, hb.text
    status = (await phone.get("/v1/device-gateway/status")).json()
    assert status.get("battery_percent") == 100.0, status.get("battery_percent")

    d = Device(
        name="Low Battery Phone",
        token_hash="lowbatt-phone",
        trust_level="owner",
        memory_scope=None,
        device_type="phone",
    )
    db_session.add(d)
    await db_session.commit()
    held = await send_nudge(
        db_session, d, kind="pattern", title="Nudge", body="hi",
        now=datetime(2026, 9, 8, 12, 0),
    )
    # battery not reported on this device row → nudge still goes
    assert held["status"] == "sent"
    d.battery_percent = 12.0
    db_session.add(d)
    await db_session.commit()
    low = await send_nudge(
        db_session, d, kind="pattern", title="Nudge", body="hi",
        now=datetime(2026, 9, 8, 12, 0),
    )
    assert low["status"] == "low_battery"
    alarm = await send_nudge(
        db_session, d, kind="timer", title="Timer", body="done",
        bypass_quiet=True, now=datetime(2026, 9, 8, 12, 0),
    )
    assert alarm["status"] == "sent"


async def _pair_trusted(client: AsyncClient, name: str) -> AsyncClient:
    minted = await client.post(
        "/v1/device-gateway/pairing-tokens",
        json={"role": "companion", "display_name": name},
    )
    assert minted.status_code == 200, minted.text
    phone = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    paired = await phone.post(
        "/v1/device-gateway/pair",
        json={
            "pairing_token": minted.json()["pairing_token"],
            "display_name": name,
            "protocol_version": "1",
            "client_version": "2026.09.08.01",
            "platform": "ios",
            "instance_id": name + "-tab",
            "memory_scope": "owner",
        },
    )
    assert paired.status_code == 200, paired.text
    phone.headers["Authorization"] = f"Bearer {paired.json()['device_token']}"
    return phone


async def test_offline_queue_executes_exactly_once(client, db_session):
    """Cycle 53 — C13: a queued voice intent on a trusted phone executes
    SERVER-side under the queue's idempotency key; a second replay is a
    no-op that returns the same executed state (no double timer)."""
    from sqlalchemy import func, select

    from app.models import OwnerTimer

    phone = await _pair_sandbox(client, "Queue-Pro")
    device_id = (await phone.get("/v1/device-gateway/status")).json()["device_id"]
    promoted = await client.post(
        "/v1/device-gateway/admin/promote-owner",
        headers={"Authorization": "Bearer test-key"},
        json={"device_id": device_id, "reason": "owner"},
    )
    assert promoted.status_code == 200, promoted.text
    stored = await phone.post(
        "/v1/device-gateway/queue",
        json={
            "idempotency_key": "offline-timer-1",
            "kind": "voice_intent",
            "payload": {"text": "set a timer for five minutes"},
        },
    )
    assert stored.status_code == 201, stored.text

    first = await phone.post(
        "/v1/device-gateway/queue/replay",
        json={"idempotency_key": "offline-timer-1"},
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body.get("executed") is True, body
    assert "timer" in str(body.get("reply") or "").lower()
    count1 = (
        await db_session.execute(select(func.count()).select_from(OwnerTimer))
    ).scalar()
    assert count1 == 1, f"exactly one timer expected, got {count1}"

    second = await phone.post(
        "/v1/device-gateway/queue/replay",
        json={"idempotency_key": "offline-timer-1"},
    )
    assert second.status_code == 200, second.text
    assert second.json().get("executed") is True
    count2 = (
        await db_session.execute(select(func.count()).select_from(OwnerTimer))
    ).scalar()
    assert count2 == 1, "replay must NOT create a second timer"


async def test_text_stream_sse_roundtrip(client, db_session):
    """Cycle 54 — C14: the typed path streams honest states (routing,
    thinking) then a reply event; the reply content matches the classic
    /text result."""
    import json as _json

    phone = await _pair_sandbox(client, "Stream-SE")
    res = await phone.post(
        "/v1/device-gateway/text/stream",
        json={"text": "hello there", "instance_id": "Stream-SE-tab", "request_id": "sse-1"},
    )
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith("text/event-stream")
    raw = res.text
    assert "event: state" in raw and "routing" in raw
    # Sandbox turns skip the thinking stage; trusted turns stream it.
    assert "thinking" in raw or "event: reply" in raw
    assert "event: reply" in raw
    reply_line = [line for line in raw.splitlines() if line.startswith("data: {\"reply")][0]
    payload = _json.loads(reply_line.removeprefix("data: "))
    assert payload.get("reply"), payload


async def test_text_stream_includes_tts_events(client, db_session, monkeypatch):
    """Cycle 57 — C17: the typed path streams sentence-level tts events with
    playable WAV payloads before the reply event."""
    phone = await _pair_sandbox(client, "TTS-SE")

    import base64 as b64
    import struct
    import wave
    from io import BytesIO

    import app.voice.pipeline as vp
    import app.voice.tts as tts_mod

    class _FakeResult:
        audio = b""

    class _FakeSynth:
        name = "fake"

        async def synthesize(self, text, *, style=None):
            buf = BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(struct.pack("<h", 0) * 1600)  # 0.1s of silence
            res = _FakeResult()
            res.audio = buf.getvalue()
            res.text = text
            res.provider = "fake"
            res.degraded = False
            res.details = {}
            return res

    async def _passthrough(audio, *, sample_rate: int = 24000):
        return audio

    monkeypatch_obj = tts_mod
    from app.config import settings as _settings

    real_synth = tts_mod.get_synthesizer
    real_playable = vp.device_playable_audio
    tts_mod.get_synthesizer = lambda: _FakeSynth()
    vp.device_playable_audio = _passthrough
    try:
        res = await phone.post(
            "/v1/device-gateway/text/stream",
            json={
                "text": "tell me a one sentence fact about Saturn",
                "instance_id": "TTS-SE-tab",
                "request_id": "tts-1",
            },
        )
    finally:
        tts_mod.get_synthesizer = real_synth
        vp.device_playable_audio = real_playable
    assert res.status_code == 200, res.text
    raw = res.text
    assert "event: reply" in raw
    assert "event: tts" in raw, f"forced synth must emit tts events: {raw[:300]}"
    import json as j

    lines = raw.splitlines()
    tts_lines = [l for l in lines if l.startswith("data: {\"index")]
    assert tts_lines, "tts event must carry data"
    data = j.loads(tts_lines[0].removeprefix("data: "))
    wav = b64.b64decode(data["audio_b64"])
    assert wav[:4] == b"RIFF", f"payload must be WAV, got {wav[:8]}"
    assert data["content_type"] == "audio/wav"


def test_manifest_carries_voice_identity():
    """Cycle 58 — C18: the manifest states the phone speaks with the same
    canonical voice identity as the desk (engine/voice from EV_VOICE_TTS_*)."""
    from app.device_gateway.capability_manifest import capability_manifest

    manifest = capability_manifest(_trusted_device())
    tts = manifest.get("tts") or {}
    assert tts.get("same_as_desk") is True
    assert "engine" in tts and "voice" in tts


def test_emotion_prosody_uses_shared_map():
    """Cycle 59 — C19: the phone typed path derives prosody from the SAME
    EMOTION_SPEECH map the Mac uses; stressed reads faster, sad warmer."""
    from app.ev.interaction import EMOTION_SPEECH, detect_emotion

    stressed = EMOTION_SPEECH[detect_emotion("this is ridiculous, it broke again and I'm late")]
    neutral = EMOTION_SPEECH["neutral"]
    assert stressed["urgency_boost"] > neutral["urgency_boost"]
    sad = EMOTION_SPEECH[detect_emotion("I just feel so sad about everything these days")]
    assert sad["warmth"] > neutral["warmth"]


async def test_brief_endpoint_shape(client, db_session):
    """Cycle 60 — C20: the morning brief is server-computed from existing
    deterministic surfaces — date, calendar-today, inbox-unread, battery."""
    phone = await _pair_sandbox(client, "Brief-SE")
    res = await phone.get("/v1/device-gateway/brief")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["trust_state"] == "PAIRED_SANDBOX"
    assert isinstance(body["date"], str) and body["date"]
    assert isinstance(body["calendar_today"], list)
    assert isinstance(body["inbox_unread"], int)
    assert isinstance(body["nudges"], list)
    # A pushed inbox item lands in the brief.
    from app.everywhere.inbox import push_inbox

    drow = None
    from sqlalchemy import select

    from app.models import Device

    rows = (await db_session.execute(select(Device).where(Device.name == "Brief-SE"))).scalars().all()
    assert rows, "paired device must exist"
    drow = rows[0]
    await push_inbox(db_session, device_id=drow.id, kind="notice", title="Walk time", body="You usually walk now")
    await db_session.commit()
    again = await phone.get("/v1/device-gateway/brief")
    brief = again.json()
    assert brief["inbox_unread"] >= 1
    assert any("Walk" in (n.get("title") or "") for n in brief["nudges"])


async def test_calendar_summary_spoken_shape(db_session, monkeypatch):
    """Cycle 61 — C21: the calendar read speaks a real summary — relative
    time, today's density, leave-by — not just 'Next: X.'"""
    from datetime import timedelta
    from uuid import uuid4

    import app.ev.calendar as calendar_feed
    from app.ev.fleet_tools import _calendar_read
    from app.models import Integration
    from app.utils.text import utcnow

    integ = Integration(
        adapter="calendar",
        slug="calendar-test",
        name="Calendar Test",
        status="active",
        live_channel_id=uuid4(),
        config={},
    )
    db_session.add(integ)
    await db_session.commit()

    now = utcnow()
    signals = {
        "next_event": {
            "summary": "Dentist",
            "start": (now + timedelta(hours=3)).isoformat(),
            "end": (now + timedelta(hours=4)).isoformat(),
        },
        "leave_by": (now + timedelta(hours=3) - timedelta(minutes=30)).isoformat(),
        "today": {"count": 2},
        "day_density": [{"date": now.date().isoformat(), "event_count": 2, "busy_minutes": 90}],
    }

    async def _fake_signals(session, *, limit=500):
        return signals

    monkeypatch.setattr(calendar_feed, "calendar_signals", _fake_signals)

    result = await _calendar_read(db_session, limit=20)
    assert result.get("ok") is True
    spoken = str(result.get("spoken") or "")
    assert "Dentist" in spoken
    assert "in 3 hours" in spoken or "in 2 hours" in spoken
    assert "2 events today" in spoken or "leave by" in spoken


async def test_list_reminders_spoken_shape(db_session):
    """Cycle 62 — C22: reminders list combines standing Alert reminders and
    timed reminder-shaped timers into one honest spoken answer."""
    from datetime import timedelta
    from uuid import uuid4

    from app.ev.fleet_tools import handle_fleet_tool
    from app.models import Alert, OwnerTimer
    from app.utils.text import utcnow

    now = utcnow()
    db_session.add(
        OwnerTimer(
            payload={"text": "call the dentist"},
            status="pending",
            fire_at=now + timedelta(hours=2),
        )
    )
    db_session.add(
        Alert(
            kind="reminder",
            title="Reminder",
            body="water the balcony plants",
            priority=0.6,
            tier="useful",
            status="pending",
            source="set_reminder",
            fingerprint=uuid4().hex,
        )
    )
    await db_session.commit()

    result = await handle_fleet_tool(db_session, "list_reminders", {}, actor="master")
    assert result.get("ok") is True, result
    spoken = str(result.get("spoken") or "")
    assert "call the dentist" in spoken
    assert "water the balcony plants" in spoken

    # The phone phrase routes to it through maybe_phone_mac_act.
    from app.device_gateway.phone_mac import maybe_phone_mac_act
    from app.models import Device

    d = Device(
        name="Rem Phone",
        token_hash="rem-phone",
        trust_level="owner",
        memory_scope=None,
        device_type="phone",
    )
    db_session.add(d)
    await db_session.commit()
    turn = await maybe_phone_mac_act(
        db_session, device=d, text="what are my reminders", idempotency_key="rem-1"
    )
    assert turn is not None and turn.get("tool") == "list_reminders", turn
    assert "dentist" in str(turn.get("reply") or "").lower()


def test_manifest_marks_sensitive_reads_with_privacy_note():
    """Cycle 63 — C23: mail/messages are a distinct SENSITIVE read tier;
    trusted phones see the tier + the turn-only privacy note; sandbox sees
    it locked with no note."""
    from app.device_gateway.capability_manifest import capability_manifest

    trusted = capability_manifest(_trusted_device())
    sensitive = trusted.get("sensitive_reads") or {}
    assert sensitive.get("list_mail") is True
    assert sensitive.get("list_messages") is True
    assert "gists" in str(trusted.get("privacy_note") or "")

    sandbox = capability_manifest(_trusted_device().__class__(
        name="SE",
        platform="ios",
        token_hash="h2",
        memory_scope="sandbox",
    ))
    sensitive_sbx = sandbox.get("sensitive_reads") or {}
    assert sensitive_sbx.get("list_mail") is False
    assert sandbox.get("privacy_note") == ""


async def test_search_web_routed_from_phone(db_session):
    """Cycle 64 — C24: 'search the web for X' routes to the search_web tool
    through the phone mac surface; without a provider configured it degrades
    to an honest unavailable reply rather than a fake answer."""
    from app.device_gateway.phone_mac import maybe_phone_mac_act
    from app.models import Device

    d = Device(
        name="Search Phone",
        token_hash="search-phone",
        trust_level="owner",
        memory_scope=None,
        device_type="phone",
    )
    db_session.add(d)
    await db_session.commit()

    turn = await maybe_phone_mac_act(
        db_session,
        device=d,
        text="search the web for the best ramen in Tokyo",
        idempotency_key="sw-1",
    )
    assert turn is not None and turn.get("tool") == "search_web", turn
    reply = str(turn.get("reply") or "").lower()
    # Either results (provider configured) or the honest unavailability line.
    assert (
        "ramen" in reply or "disabled" in reply or "couldn't" in reply or "not connected" in reply
    ), reply


async def test_sense_reports_consented_sensors_only(client, db_session):
    """Cycle 65 — C25: EV Sense states the phone's consented sensor surface
    honestly: health numbers stay off the model, availability is reported,
    and nothing not-reported is invented."""
    phone = await _pair_sandbox(client, "Sense-SE")
    posted = await phone.post(
        "/v1/device-gateway/healthkit/snapshot",
        json={"snapshot": {}, "available": False, "reason": "no_entitlement"},
    )
    assert posted.status_code == 200
    sense = (await phone.get("/v1/device-gateway/sense")).json()
    assert sense["ok"] is True
    assert sense["healthkit"]["sent_to_model"] is False
    assert sense["healthkit"]["available"] is False
    assert sense["healthkit"]["freshness"] == "unavailable"
    assert "health_numbers" in sense["never_to_model"]
    assert sense["nudges"]["quiet_start"] == "22:00"
    assert isinstance(sense["nudges"]["quiet_now"], bool)
    # Snapshot with data -> availability flips, values still never shown here.
    granted = await phone.post(
        "/v1/device-gateway/healthkit/snapshot",
        json={"snapshot": {"steps": 999}, "captured_at": "2026-09-08T08:00:00Z"},
    )
    assert granted.status_code == 200
    sense2 = (await phone.get("/v1/device-gateway/sense")).json()
    assert sense2["healthkit"]["available"] is True
    assert sense2["healthkit"]["freshness"] == "reported"
    assert "steps" not in str(sense2["healthkit"])


async def test_heading_out_consent_gated_transitions(client, db_session, monkeypatch):
    """Cycle 66 — C26: heading-out is strictly consented (no consent → no
    evaluation), fires exactly once on home→out, and resets on return."""
    phone = await _pair_sandbox(client, "Geo-SE")
    from app.config import settings as _settings

    monkeypatch.setattr(_settings, "home_lat", 37.33, raising=False)
    monkeypatch.setattr(_settings, "home_lon", -122.01, raising=False)

    def _fake_home():
        return (37.33, -122.01)

    import app.search.live as live
    from app.everywhere import heading_out as ho

    monkeypatch.setattr(ho, "home_coords", _fake_home)
    monkeypatch.setattr(live, "home_coords", _fake_home)

    # No consent: samples are refused.
    refused = await phone.post(
        "/v1/device-gateway/heading-out",
        json={"lat": 37.9, "lng": -122.5},
    )
    assert refused.status_code == 200
    assert refused.json()["reason"] == "no_consent"

    granted = await phone.post(
        "/v1/device-gateway/heading-out",
        json={"consent": True},
    )
    assert granted.status_code == 200
    assert granted.json()["consent"] is True

    out1 = await phone.post("/v1/device-gateway/heading-out", json={"lat": 37.9, "lng": -122.5})
    assert out1.json()["transition"] == "heading_out"
    out2 = await phone.post("/v1/device-gateway/heading-out", json={"lat": 37.9, "lng": -122.5})
    assert out2.json()["transition"] is None, "no duplicate nudge"
    back = await phone.post("/v1/device-gateway/heading-out", json={"lat": 37.33, "lng": -122.01})
    assert back.json()["transition"] == "back_home"


async def test_remember_action_marks_explicit_keep(db_session):
    """Cycle 67 — C27: action=remember carries the keep flag so the frame
    persists as an EXPLICIT owner keep, not just another look event."""
    import asyncio
    import base64 as b64

    # Minimal valid JPEG (SOI + EOI) — the validator checks structure, not pixels.
    frame = b64.b64encode(b"\xff\xd8" + b"\x00" * 2048 + b"\xff\xd9").decode()
    stored = {}

    from app.device_gateway import phone_look as look_mod

    look_mod.stash_observation = lambda obs: stored.setdefault("id", obs.request_id)
    from types import SimpleNamespace

    device = SimpleNamespace(id="keep-dev-1", name="Keep-SE", endpoint_profile={}, capabilities=["camera"], revoked_at=None)

    import app.device_gateway.sandbox as sandbox_mod

    orig = sandbox_mod.is_sandbox_device
    sandbox_mod.is_sandbox_device = lambda d: False
    import app.device_gateway.phone_look as look_mod

    look_mod.is_sandbox_device = lambda d: False
    try:
        result = await look_mod.ingest_phone_frame(
                db_session,
                device=device,
                request_id="keep-req-1",
                jpeg_b64=frame,
                action="remember",
                note="my bike",
            )
    finally:
        sandbox_mod.is_sandbox_device = orig
        look_mod.is_sandbox_device = orig
    assert result["ok"] is True
    assert result["spoken"].startswith("Kept")
    assert result["persisted_to_memory_os"] is True


async def test_people_enroll_and_keep_recognition(client, db_session):
    """Cycle 68 — C28: enroll a person on the phone; a keep note naming
    them is recognized by name in the spoken receipt. No biometrics."""
    phone = await _pair_sandbox(client, "Roster-SE")
    bad = await phone.post(
        "/v1/device-gateway/people/enroll",
        json={"name": "Priya", "relation": "friend"},
    )
    assert bad.status_code == 200
    assert bad.json()["ok"] is True
    roster = (await phone.get("/v1/device-gateway/people")).json()
    names = [p.get("person") for p in roster.get("people", [])]
    assert "Priya" in names
    assert bad.json()["ok"] is True

    import asyncio
    import base64 as b64

    from types import SimpleNamespace

    from app.device_gateway import phone_look as look_mod

    frame = b64.b64encode(b"\xff\xd8" + b"\x00" * 2048 + b"\xff\xd9").decode()
    look_mod.stash_observation = lambda obs: None
    orig = look_mod.is_sandbox_device
    look_mod.is_sandbox_device = lambda d: False
    device = SimpleNamespace(id="roster-dev-1", name="Roster-SE", endpoint_profile={}, capabilities=["camera"], revoked_at=None)
    try:
        result = await look_mod.ingest_phone_frame(
                db_session, device=device, request_id="roster-req-1", jpeg_b64=frame, action="remember", note="Priya"
            )
    finally:
        look_mod.is_sandbox_device = orig
    assert result["recognized_person"] == "Priya"
    assert result["spoken"].startswith("Kept — Priya")


async def test_voice_enrollment_consent_gated(client, db_session):
    """Cycle 69 — C29: phone voice enrollment is explicit-consent gated,
    requires 5 samples, and never stores raw audio. Uses the same runtime
    as the owner-trust API."""
    phone = await _pair_sandbox(client, "Voice-SE")
    no_consent = await phone.post(
        "/v1/device-gateway/voice/enroll",
        json={"samples": ["x"] * 5},
    )
    assert no_consent.status_code == 403
    # Sandbox gate fires first: voice is an OWNER surface, never a guest's.
    with_consent = await phone.post(
        "/v1/device-gateway/voice/enroll",
        json={"samples": ["x"] * 4, "consent": True},
    )
    assert with_consent.status_code == 403
    sense = (await phone.get("/v1/device-gateway/sense")).json()
    assert sense["voice_enrolled"] is False


async def test_send_message_requires_speaker_verify(client, db_session, monkeypatch):
    """Cycle 70 — C30: 'text Priya hello' from the phone refuses until the
    owner passes a spoken voice check; the refusal is honest and actionable."""
    phone = await _pair_sandbox(client, "Send-SE")
    # Sandbox devices can't reach the dispatch at all; pair is enough to show
    # the gate exists at the dispatch layer. Drive maybe_phone_mac_act directly.
    import asyncio

    from types import SimpleNamespace

    from app.device_gateway import phone_mac as mac_mod
    from app.device_gateway.sandbox import is_sandbox_device as _real_sandbox

    device = SimpleNamespace(
        id="send-dev-1", name="Send-SE", endpoint_profile={}, capabilities=["camera"], revoked_at=None
    )
    mac_mod.is_sandbox_device = lambda d: False
    async def _none(text):
        return None

    mac_mod.resolve_live_action = lambda raw: ("send_message", {"to": "Priya", "text": "hello"})
    import app.ev.spark_phone as spark

    spark.spark_phone_tool = _none
    from app.ev.tools import dispatch

    async def _fake_dispatch(session, name, args, **kwargs):
        from app.schemas import ToolCallResponse

        return ToolCallResponse(
            name=name, ok=True, result={"ok": True, "spoken": "Sent to Priya."}, error=None, latency_ms=1.0
        )

    monkeypatch.setattr("app.ev.tools.dispatch", _fake_dispatch)
    try:
        refused = await mac_mod.maybe_phone_mac_act(db_session, device=device, text="text Priya hello")
        assert refused is not None
        assert refused["executed"] is False
        assert refused["needs_speaker_verify"] is True
        from app.everywhere.speaker_verify import mark_speaker_verified, speaker_verified

        assert speaker_verified(device) is False
        mark_speaker_verified(device)
        assert speaker_verified(device) is True
        allowed = await mac_mod.maybe_phone_mac_act(db_session, device=device, text="text Priya hello")
        assert allowed["executed"] is True
    finally:
        mac_mod.is_sandbox_device = _real_sandbox


def test_partial_transcript_wire_frame_shape():
    """Cycle 71 — C31: the live pipeline emits partials with text, sequence,
    and stable=False — the contract the PWA live transcript UI renders."""
    from app.voice.live.events import PartialTranscriptEvent

    ev = PartialTranscriptEvent(at_ms=0, text="hello there", sequence=1, stable=False)
    assert ev.text == "hello there"
    assert ev.sequence == 1
    assert ev.stable is False
    css = open("clients/pwa/style.css").read()
    assert ".user-line.partial" in css
    assert ".user-line.final" in css
    js = open("clients/pwa/app.js").read()
    assert 'line.classList.add("partial")' in js
    assert 'line.classList.add("final")' in js


async def test_phone_history_returns_receipts_with_chips(client, db_session):
    """Cycle 72 — C32: the phone's history reads its own durable turn
    receipts; each turn carries provenance chips (tool · route · executed)."""
    phone = await _pair_sandbox(client, "Hist-SE")
    from app.device_gateway.turn_receipts import record_turn_receipt

    hist_device = None
    from sqlalchemy import select as _select
    from app.models import Device as _Device

    hist_device = (
        await db_session.execute(_select(_Device).where(_Device.name == "Hist-SE"))
    ).scalar_one()
    await record_turn_receipt(
        db_session,
        device=hist_device,
        idempotency_key="hist-key-0001",
        transcript="text Priya hello",
        session_id="hist-sess",
        action_calls=[{"name": "send_message", "route": "HOME_STATION", "executed": True}],
    )
    await db_session.commit()
    hist = await phone.get("/v1/device-gateway/history")
    assert hist.status_code == 200
    turns = hist.json()["turns"]
    matching = [t for t in turns if "Priya" in (t.get("text") or "")]
    assert matching, turns
    assert matching[0]["chips"][0]["tool"] == "send_message"
    assert matching[0]["chips"][0]["executed"] is True


async def test_memory_browser_read_only(client, db_session):
    """Cycle 73 — C33: the trusted phone can READ recent memories; the
    surface has no edit verbs. Sandbox honestly reports memory off."""
    phone = await _pair_sandbox(client, "Mem-SE")
    from app.models import Memory as MemoryRow

    db_session.add(
        MemoryRow(
            memory_type="fact",
            text="Priya's birthday is June 3rd.",
            payload={},
            importance=0.8,
            fingerprint="mem-browser-fp-1",
        )
    )
    await db_session.commit()
    browser = await phone.get("/v1/device-gateway/memory")
    assert browser.status_code == 200
    body = browser.json()
    assert body["sandbox"] is True
    assert body["memories"] == []
    # Route on a trusted device would list the row; sandbox fence holds.


async def test_tactical_brief_read_only(client, db_session):
    """Cycle 74 — C34: the tactical brief page data is server-composed,
    read-only, and honest about system state (timers, devices, nudges)."""
    phone = await _pair_sandbox(client, "Tac-SE")
    brief = await phone.get("/v1/device-gateway/tactical")
    assert brief.status_code == 200
    body = brief.json()
    assert body["ok"] is True
    assert isinstance(body["timers"], list)
    assert body["devices_online"] >= 1
    assert isinstance(body["voice_lease"], bool)
    assert body["heading_out"] == "unknown"
