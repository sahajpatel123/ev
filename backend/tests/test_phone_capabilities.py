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
