"""Voice interaction layer: reconnect, hold, barge-in, quiet hours, continuity.

Drives the real live session, Grok/OpenAI realtime bridge, live/open HTTP
door, grok tool runner, and callout entry points. Upstream sockets are local
doubles — these tests do not claim a live OpenAI or xAI audio session.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import timedelta
from pathlib import Path

from httpx import AsyncClient

from app.ev.callouts import emit_callout
from app.ev.policy import HOLD_LINE, evaluate_policy
from app.ev.workbench import hud_card
from app.voice.live.events import (
    BargeInEvent,
    ErrorEvent,
    HudEvent,
    ReplyEvent,
    TtsChunkEvent,
)
from app.voice.live.grok_voice import GrokVoiceBridge
from app.voice.live.layer import (
    classify_live_intent,
    hold_result,
    reset_live_registry,
    spoken_hardware_failure,
    spoken_missing_key,
    spoken_provider_connect_failed,
    spoken_provider_disconnect,
)
from app.voice.live.session import LiveSession
from app.voice.live.transport import _grok_tool_runner, serve_live_websocket
from tests.test_gateway_xai import _acknowledge_session, _FakeRealtime
from tests.test_voice_lifecycle import grant_voice_consent
from tests.test_voice_live_ws import FakeWebSocket


def _drain(session: LiveSession) -> list:
    items = []
    while not session.outbound.empty():
        items.append(session.outbound.get_nowait())
    return items


def test_honesty_and_intent_classifier_are_whole_turn() -> None:
    assert classify_live_intent("pause") == "pause"
    assert classify_live_intent("resume") == "resume"
    assert classify_live_intent("cancel that") == "cancel"
    assert classify_live_intent("what can you do") == "capability"
    assert classify_live_intent("please wait for Ned") == "none"
    assert classify_live_intent("don't pause the print") == "none"
    assert "disconnected" in spoken_provider_disconnect("openai").lower()
    assert "EV_XAI_API_KEY" in spoken_missing_key("xai")
    assert "EV_OPENAI_API_KEY" in spoken_missing_key("openai")
    assert "retry" in spoken_provider_connect_failed("xai").lower()
    assert "hear" in spoken_hardware_failure("mic").lower()


async def test_reconnect_after_long_mute_rearms_realtime() -> None:
    reset_live_registry()
    fakes: list[_FakeRealtime] = []

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        fake = _FakeRealtime()
        fakes.append(fake)
        return fake

    session = LiveSession(session_id="mute-1", device_id="mac", backchannel_enabled=False)
    bridge = GrokVoiceBridge(
        on_event=session.emit,
        connect=connect,
        api_key="test",
        now_ms=session.now,
        reconnect_delay_s=0.01,
    )
    session.grok_voice = bridge
    await bridge.start()
    assert len(fakes) == 1
    await session.handle_client({"type": "control", "action": "mute"})
    assert session._muted is True
    assert not session._closed
    bridge._ws = None
    await session.handle_client({"type": "control", "action": "resume"})
    assert session._muted is False
    assert not session._closed
    assert len(fakes) == 2
    assert fakes[1].sent[0]["type"] == "session.update"
    session.close()


async def test_provider_disconnect_is_nonfatal_and_reconnects() -> None:
    reset_live_registry()
    fakes: list[_FakeRealtime] = []

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        fake = _FakeRealtime()
        fakes.append(fake)
        return fake

    session = LiveSession(session_id="disc-1", device_id="mac", backchannel_enabled=False)
    bridge = GrokVoiceBridge(
        on_event=session.emit,
        connect=connect,
        api_key="test",
        now_ms=session.now,
        reconnect_delay_s=0.01,
    )
    session.grok_voice = bridge
    await bridge.start()
    await fakes[0].incoming.put(None)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and len(fakes) < 2:
        await asyncio.sleep(0.02)
    events = _drain(session)
    disconnects = [
        event
        for event in events
        if isinstance(event, ErrorEvent) and event.code == "realtime_disconnect"
    ]
    assert disconnects, [getattr(event, "type", None) for event in events]
    assert disconnects[0].fatal is False
    assert not session._closed
    assert len(fakes) >= 2
    session.close()


async def test_approval_hold_keeps_audio_loop_alive() -> None:
    reset_live_registry()
    session = LiveSession(
        session_id="hold-1",
        device_id="mac",
        conversation_id="thread-1",
        backchannel_enabled=False,
    )
    decision = evaluate_policy(
        "place_call",
        actor="master",
        channel="voice",
        arguments={"name": "Ned"},
    )
    payload = hold_result(decision, name="place_call", arguments={"name": "Ned"})
    started = time.monotonic()
    await session.apply_approval_hold(payload)
    assert time.monotonic() - started < 0.2
    assert not session._closed
    assert session._approval_hold is not None
    events = _drain(session)
    hud = next(event for event in events if isinstance(event, HudEvent))
    assert hud.card["meta"]["kind"] == "approval_hold"
    assert hud.card["meta"]["wake_verification_insufficient"] is True
    assert hud.card["meta"]["confirmation_channel"] == "hud_or_biometric"
    assert HOLD_LINE in (payload.get("spoken") or "")
    replies = [event for event in events if isinstance(event, ReplyEvent)]
    assert any(HOLD_LINE in (event.text or "") for event in replies)
    await session.handle_client(b"\x00\x01" * 800)
    assert not session._closed
    session.close()


async def test_grok_tool_runner_holds_without_waiting_for_tap(db_session) -> None:
    from tests.test_pol_policy import _unlock_life

    reset_live_registry()
    await _unlock_life(db_session)
    session = LiveSession(session_id="hold-tool", device_id="mac", backchannel_enabled=False)
    runner = _grok_tool_runner(actor="voice", device_id=None, live=session)
    started = time.monotonic()
    raw = await asyncio.wait_for(
        runner("place_call", {"name": "Ned"}, "call-1"),
        timeout=4,
    )
    assert time.monotonic() - started < 3.5
    body = json.loads(raw)
    assert body.get("error") == "confirmation_required"
    assert not session._closed
    events = _drain(session)
    assert any(isinstance(event, HudEvent) and event.kind in {"progress", "approval_hold"} for event in events)
    assert any(isinstance(event, HudEvent) and event.kind == "approval_hold" for event in events)
    await session.handle_client({"type": "control", "action": "attentive"})
    assert not session._closed
    session.close()


async def test_barge_in_cancels_speech_not_durable_jobs() -> None:
    reset_live_registry()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow(text: str, envelope):
        del text, envelope
        started.set()
        try:
            await asyncio.sleep(30)
            yield ReplyEvent(at_ms=0, text="should not land")
        except asyncio.CancelledError:
            cancelled.set()
            raise

    session = LiveSession(session_id="barge-1", respond=slow, backchannel_enabled=False)
    await session.handle_client(
        {"type": "text", "text": "explain this at length please", "commit": True}
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    await session.handle_client({"type": "control", "action": "barge_in"})
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    events = _drain(session)
    assert any(isinstance(event, BargeInEvent) for event in events)
    assert session._durable_jobs_cancelled is False
    assert not session._closed
    session.close()


async def test_quiet_hours_suppress_proactive_live_speech(monkeypatch) -> None:
    reset_live_registry()
    monkeypatch.setattr("app.ev.ev_sense.quiet_hours_active", lambda now=None: True)
    session = LiveSession(session_id="quiet-1", device_id="mac", backchannel_enabled=False)
    await session.speak_proactive("Print job finished.")
    quiet_events = _drain(session)
    assert not any(
        isinstance(event, (ReplyEvent, TtsChunkEvent))
        and "Print job finished." in (getattr(event, "text", None) or "")
        for event in quiet_events
    )
    await session.speak_proactive("Something's on fire.", emergency=True)
    emergency_events = _drain(session)
    assert any(
        isinstance(event, ReplyEvent) and "fire" in event.text.lower()
        for event in emergency_events
    )
    session.close()


async def test_owner_timer_rings_on_live_during_quiet_hours(db_session, monkeypatch) -> None:
    from uuid import UUID

    from sqlalchemy import select

    from app.config import settings
    from app.ev.timers import due_scan, start_timer
    from app.models import Callout, OwnerTimer
    from app.utils.text import utcnow
    from app.voice.live.layer import register_live

    reset_live_registry()
    monkeypatch.setattr(settings, "timezone", "UTC")
    monkeypatch.setattr(settings, "quiet_hours_start", "00:00")
    monkeypatch.setattr(settings, "quiet_hours_end", "23:59")
    live = LiveSession(
        session_id="timer-quiet-fire",
        device_id="mac",
        backchannel_enabled=False,
    )
    register_live(live)
    started = await start_timer(db_session, minutes=1, text="one minute")
    assert started["ok"] is True
    assert started["spoken"] == "Timer set for one minute."
    row = await db_session.get(OwnerTimer, UUID(started["id"]))
    assert row is not None
    row.fire_at = utcnow() - timedelta(seconds=1)
    await db_session.commit()

    scanned = await due_scan(db_session)
    assert scanned["fired"] == 1
    events = _drain(live)
    assert any(
        isinstance(event, ReplyEvent) and "one minute" in (event.text or "")
        for event in events
    )
    assert any(
        isinstance(event, HudEvent) and event.kind == "callout"
        for event in events
    )
    callout = (
        await db_session.execute(select(Callout).where(Callout.source == "15"))
    ).scalar_one()
    assert callout.spoken is True
    again = await due_scan(db_session)
    assert again["fired"] == 0
    live.close()


async def test_emit_callout_does_not_inject_tts_during_quiet_hours(
    db_session, monkeypatch
) -> None:
    reset_live_registry()
    from app.config import settings

    monkeypatch.setattr(settings, "timezone", "UTC")
    monkeypatch.setattr(settings, "quiet_hours_start", "00:00")
    monkeypatch.setattr(settings, "quiet_hours_end", "23:59")
    session = LiveSession(session_id="callout-1", device_id="mac", backchannel_enabled=False)
    row = await emit_callout(
        db_session, "Print job finished.", source="print", hud={"title": "Print"}
    )
    assert row.spoken is False
    events = _drain(session)
    assert not any(
        isinstance(event, (ReplyEvent, TtsChunkEvent))
        and "Print job finished." in (getattr(event, "text", None) or "")
        for event in events
    )
    session.close()


async def test_cross_device_live_open_shares_conversation_id(client: AsyncClient) -> None:
    reset_live_registry()
    await grant_voice_consent(client)
    first = await client.post("/v1/voice/live/open", json={"device_id": "mac-continuity"})
    second = await client.post("/v1/voice/live/open", json={"device_id": "phone-continuity"})
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    a = first.json()
    b = second.json()
    assert a["conversation_id"]
    assert a["conversation_id"] == b["conversation_id"]
    assert a["session_id"] != b["session_id"]
    live = LiveSession(
        session_id=a["session_id"],
        conversation_id=a["conversation_id"],
        device_id="mac-continuity",
        tts_device_id="phone-continuity",
        backchannel_enabled=False,
    )
    ready = live.ready_event().as_dict()
    assert ready["config"]["device_id"] == "mac-continuity"
    assert ready["config"]["tts_device_id"] == "phone-continuity"
    assert ready["conversation_id"] == a["conversation_id"]
    live.close()


async def test_spoken_capability_discovery_uses_protocol_sheet() -> None:
    reset_live_registry()

    async def capability_reply(*, include_refused: bool = False):
        del include_refused
        return {
            "reply": "I can list enabled, setup-required, and refused protocols.",
            "hud": hud_card(
                "Protocols",
                "Sheet is available.",
                {"kind": "protocols"},
            ),
        }

    session = LiveSession(
        session_id="cap-1",
        capability_reply=capability_reply,
        backchannel_enabled=False,
    )
    await session.handle_client({"type": "text", "text": "what can you do", "commit": True})
    events = _drain(session)
    assert any(
        isinstance(event, ReplyEvent) and "protocols" in event.text.lower()
        for event in events
    )
    assert any(isinstance(event, HudEvent) and event.kind == "protocols" for event in events)
    session.close()


async def test_empty_live_projection_uses_truthful_no_tools_line() -> None:
    reset_live_registry()

    async def capability_reply(*, include_refused: bool = False):
        del include_refused
        return {
            "reply": "I can transcribe and chat.",
            "live_tool_projection": [],
            "realtime_tools": [],
        }

    session = LiveSession(
        session_id="cap-empty-1",
        capability_reply=capability_reply,
        backchannel_enabled=False,
    )
    await session.handle_client({"type": "text", "text": "what can you do", "commit": True})
    events = _drain(session)
    assert any(
        isinstance(event, ReplyEvent)
        and event.text
        == (
            "Live action tools are not available in this session. I can still converse, "
            "but I cannot honestly execute requests until the capability projection reconnects."
        )
        for event in events
    )
    session.close()


async def test_pause_drops_pcm_resume_restores_and_ws_stays_open() -> None:
    reset_live_registry()
    ws = FakeWebSocket()
    session = LiveSession(session_id="pause-1", device_id="mac", backchannel_enabled=False)
    server = asyncio.create_task(serve_live_websocket(ws, live=session, tick_ms=20))
    try:
        ready = await ws.next_event()
        assert ready["type"] == "ready"
        await ws.put_text({"type": "text", "text": "pause", "commit": True})
        paused = False
        for _ in range(20):
            event = await ws.next_event()
            if event.get("type") == "state" and event.get("state", {}).get("paused"):
                paused = True
                break
        assert paused
        assert not session._closed
        await ws.put_text({"type": "control", "action": "resume"})
        resumed = False
        for _ in range(20):
            event = await ws.next_event()
            if event.get("type") == "state" and event.get("state", {}).get("paused") is False:
                resumed = True
                break
        assert resumed
        assert not session._closed
    finally:
        await ws.put_disconnect()
        await asyncio.wait_for(server, timeout=4)
        session.close()


async def test_openai_realtime_function_call_runs_pol_and_continues() -> None:
    reset_live_registry()
    seen: list[tuple[str, dict, str]] = []

    async def runner(name: str, args: dict, call_id: str) -> str:
        seen.append((name, args, call_id))
        return json.dumps(
            {
                "ok": False,
                "error": "confirmation_required",
                "result": {"spoken": HOLD_LINE},
            }
        )

    fakes: list[_FakeRealtime] = []

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        fake = _FakeRealtime()
        fakes.append(fake)
        return fake

    session = LiveSession(session_id="oa-sidecar", device_id="mac", backchannel_enabled=False)
    session.run_live_tool = runner
    bridge = GrokVoiceBridge(
        on_event=session.emit,
        on_tool=runner,
        connect=connect,
        api_key="test",
        provider="openai",
        now_ms=session.now,
        approved_tool_specs=[
            {
                "name": "place_call",
                "description": "Place a call.",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"name": {"type": "string", "minLength": 1}},
                    "required": ["name"],
                },
            }
        ],
    )
    session.grok_voice = bridge
    await bridge.start()
    await bridge._handle_upstream(
        {"type": "session.updated", "session": {"tools": fakes[0].sent[0]["session"]["tools"]}}
    )
    assert [tool["name"] for tool in fakes[0].sent[0]["session"]["tools"]] == ["place_call"]
    assert fakes[0].sent[0]["session"]["tool_choice"] == "auto"
    await fakes[0].incoming.put(
        json.dumps(
            {
                "type": "response.function_call_arguments.done",
                "name": "place_call",
                "call_id": "openai-call-1",
                "arguments": json.dumps({"name": "Ned"}),
            }
        )
    )
    await asyncio.sleep(0.05)
    assert seen == [("place_call", {"name": "Ned"}, "openai-call-1")]
    assert any(item.get("type") == "conversation.item.create" for item in fakes[0].sent)
    assert any(item.get("type") == "response.create" for item in fakes[0].sent)
    assert not session._closed
    session.close()


async def test_openai_pending_confirmation_resumes_with_verified_result() -> None:
    reset_live_registry()
    fake = _FakeRealtime()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    session = LiveSession(session_id="oa-confirm", device_id="mac", backchannel_enabled=False)

    async def runner(name: str, args: dict, call_id: str) -> str:
        del name, args
        await session.apply_approval_hold(
            {
                "confirmation_required": True,
                "hold": True,
                "spoken": HOLD_LINE,
                "hud": hud_card("Confirm", HOLD_LINE, {"kind": "approval_hold"}),
                "_realtime_call_id": call_id,
            },
            speak=False,
        )
        return json.dumps(
            {
                "ok": False,
                "confirmation_required": True,
                "hold": True,
                "result": {"spoken": HOLD_LINE},
            }
        )

    bridge = GrokVoiceBridge(
        on_event=session.emit,
        on_tool=runner,
        connect=connect,
        api_key="test",
        provider="openai",
        now_ms=session.now,
        approved_tool_specs=[
            {
                "name": "place_call",
                "parameters": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
            }
        ],
    )
    session.grok_voice = bridge
    await bridge.start()
    await bridge._handle_upstream(
        {"type": "session.updated", "session": {"tools": fake.sent[0]["session"]["tools"]}}
    )
    await fake.incoming.put(
        json.dumps(
            {
                "type": "response.function_call_arguments.done",
                "name": "place_call",
                "call_id": "confirm-call-1",
                "arguments": json.dumps({"name": "Ned"}),
            }
        )
    )
    await asyncio.sleep(0.05)
    assert session._approval_hold is not None
    assert not session._closed
    await session.complete_approval_hold(
        "place_call",
        {
            "ok": True,
            "spoken": "Called Ned.",
            "evidence": {"source": "phone", "timestamp": "2026-08-17T00:00:00+00:00"},
        },
        spoken="Called Ned.",
    )
    assert session._approval_hold is None
    assert any(
        isinstance(event, HudEvent) and event.kind == "evidence"
        for event in _drain(session)
    )
    assert any(
        item.get("type") == "response.create"
        for item in fake.sent
    )
    assert any(
        item.get("type") == "conversation.item.create"
        and item.get("item", {}).get("role") == "user"
        and "Called Ned." in item["item"]["content"][0]["text"]
        for item in fake.sent
    )
    assert not session._closed
    session.close()


async def test_realtime_transcript_never_uses_pipeline_regex_fallback() -> None:
    reset_live_registry()
    seen: list[tuple[str, dict, str]] = []

    async def runner(name: str, args: dict, call_id: str) -> str:
        seen.append((name, args, call_id))
        return "{}"

    session = LiveSession(session_id="realtime-no-regex", backchannel_enabled=False)
    session.run_live_tool = runner
    session.grok_voice = GrokVoiceBridge(
        on_event=session.emit,
        provider="openai",
        api_key="test",
        approved_tool_specs=[],
    )
    from app.voice.live.events import FinalTranscriptEvent

    await session.emit(
        FinalTranscriptEvent(
            at_ms=session.now(),
            text="Call Ned",
            provider="openai-realtime",
        )
    )
    assert seen == []
    assert not session._closed
    session.close()


async def test_live_mailbox_delivers_when_socket_appears(db_session) -> None:
    from sqlalchemy import select

    from app.models import Callout
    from app.voice.live.layer import (
        LIVE_MAIL_SOURCE,
        deliver_pending_live_mail,
        speak_on_live,
    )

    reset_live_registry()
    delivered = await speak_on_live(
        "Print job finished.",
        device_id="mac-mail",
        db=db_session,
    )
    assert delivered is False
    row = (
        await db_session.execute(select(Callout).where(Callout.source == LIVE_MAIL_SOURCE))
    ).scalar_one()
    assert row.spoken is False
    session = LiveSession(
        session_id="mail-1",
        device_id="mac-mail",
        backchannel_enabled=False,
    )
    count = await deliver_pending_live_mail(db_session, session)
    assert count == 1
    events = _drain(session)
    assert any(
        isinstance(event, ReplyEvent) and "Print job finished." in (event.text or "")
        for event in events
    )
    await db_session.flush()
    await db_session.refresh(row)
    assert row.spoken is True
    session.close()


async def test_live_open_resolves_registry_uuid_or_name(
    client: AsyncClient, db_session
) -> None:
    from uuid import UUID

    from app.ev.fleet import resolve_registry_device
    from app.models import Device, VoiceSession

    reset_live_registry()
    await grant_voice_consent(client)
    device = Device(name="mac-studio", capabilities=["attention", "voice"])
    db_session.add(device)
    await db_session.flush()
    by_id = await resolve_registry_device(db_session, str(device.id))
    by_name = await resolve_registry_device(db_session, "mac-studio")
    assert by_id is not None and by_name is not None
    assert by_id.id == device.id == by_name.id
    await db_session.commit()

    opened = await client.post("/v1/voice/live/open", json={"device_id": str(device.id)})
    named = await client.post("/v1/voice/live/open", json={"device_id": "mac-studio"})
    assert opened.status_code == 201, opened.text
    assert named.status_code == 201, named.text
    assert opened.json()["session_id"] == named.json()["session_id"]
    row = await db_session.get(VoiceSession, UUID(opened.json()["session_id"]))
    assert row is not None
    assert row.device_id == str(device.id)


async def test_complete_approval_hold_does_not_double_speak() -> None:
    reset_live_registry()
    session = LiveSession(session_id="done-1", backchannel_enabled=False)
    await session.complete_approval_hold(
        "place_call", {"spoken": "Calling Ned."}, spoken="Calling Ned."
    )
    await session.complete_approval_hold(
        "place_call", {"spoken": "Calling Ned."}, spoken="Calling Ned."
    )
    replies = [
        event
        for event in _drain(session)
        if isinstance(event, ReplyEvent) and "Calling Ned." in (event.text or "")
    ]
    assert len(replies) == 1
    session.close()


async def test_mac_live_wire_contract_carries_progress_hold_and_evidence() -> None:
    """Exercise the event shapes consumed by Mac's live client."""

    reset_live_registry()
    manifest = {
        "schema_version": "ev.capability-manifest.v1",
        "enabled": [],
        "live_tool_projection": [],
    }
    session = LiveSession(
        session_id="mac-wire-1",
        conversation_id="mac-thread-1",
        device_id="mac-agent-3",
        tts_device_id="mac-agent-3",
        capability_manifest=manifest,
        backchannel_enabled=False,
    )

    ready = session.ready_event().as_dict()
    assert ready["config"]["device_id"] == "mac-agent-3"
    assert ready["config"]["tts_device_id"] == "mac-agent-3"
    assert ready["config"]["capability_manifest"] == manifest
    assert ready["config"]["realtime"]["tool_names"] == []

    await session.push_progress("set_reminder", detail="Working on the reminder.")
    progress = next(event for event in _drain(session) if isinstance(event, HudEvent))
    progress_wire = progress.as_dict()
    assert progress_wire["kind"] == "progress"
    assert progress_wire["hud"]["meta"]["kind"] == "progress"
    assert progress_wire["hud"]["meta"]["tool"] == "set_reminder"

    await session.apply_approval_hold(
        {
            "confirmation_required": True,
            "hold": True,
            "spoken": HOLD_LINE,
            "hud": hud_card(
                "Confirm on this device",
                HOLD_LINE,
                {
                    "kind": "approval_hold",
                    "tool": "place_call",
                    "arguments": {"name": "Ned"},
                },
            ),
        },
        speak=False,
    )
    hold = next(event for event in _drain(session) if isinstance(event, HudEvent))
    hold_wire = hold.as_dict()
    assert hold_wire["kind"] == "approval_hold"
    assert hold_wire["hud"]["meta"]["tool"] == "place_call"
    assert session.interaction_snapshot()["approval_hold"] is True

    evidence_payload = {
        "ok": True,
        "spoken": "Called Ned.",
        "evidence": {
            "source": "phone",
            "timestamp": "2026-08-17T00:00:00+00:00",
        },
    }
    await session.complete_approval_hold(
        "place_call",
        evidence_payload,
        spoken="Called Ned.",
    )
    after = _drain(session)
    evidence = next(event for event in after if isinstance(event, HudEvent))
    evidence_wire = evidence.as_dict()
    assert evidence_wire["kind"] == "evidence"
    assert evidence_wire["hud"]["meta"]["kind"] == "evidence"
    assert evidence_wire["hud"]["meta"]["source"] == "phone"
    assert evidence_wire["hud"]["meta"]["evidence"] == evidence_payload["evidence"]
    assert session.interaction_snapshot()["approval_hold"] is False
    session.close()


async def test_realtime_session_expiration_reconnects_without_ending_live() -> None:
    """Provider session expiry must recover like a dropped upstream socket."""

    reset_live_registry()
    fakes: list[_FakeRealtime] = []

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        fake = _FakeRealtime()
        fakes.append(fake)
        return fake

    events: list[ErrorEvent] = []

    async def on_event(event):
        if isinstance(event, ErrorEvent):
            events.append(event)

    bridge = GrokVoiceBridge(
        on_event=on_event,
        connect=connect,
        api_key="test",
        provider="openai",
        reconnect_delay_s=0.01,
        approved_tool_specs=[
            {
                "name": "start_timer",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
            }
        ],
    )
    await bridge.start()
    await _acknowledge_session(bridge, fakes[0])
    await bridge._handle_upstream(
        {
            "type": "error",
            "error": {
                "code": "session_expired",
                "message": "Realtime session expired",
            },
        }
    )
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline and len(fakes) < 2:
        await asyncio.sleep(0.01)
    assert len(fakes) >= 2
    await _acknowledge_session(bridge, fakes[1])
    assert bridge.upstream_session_ready is True
    assert bridge.upstream_tool_names == ("start_timer",)
    assert events
    assert all(event.fatal is False for event in events)
    bridge.close()


async def test_mute_and_unmute_publish_truthful_state_and_keep_session_alive() -> None:
    reset_live_registry()
    session = LiveSession(session_id="mute-state-1", device_id="mac", backchannel_enabled=False)

    await session.handle_client({"type": "control", "action": "mute"})
    muted = _drain(session)
    muted_state = next(event for event in muted if event.type == "state")
    assert muted_state.state["muted"] is True
    assert session._closed is False

    await session.handle_client(b"\x01\x00" * 800)
    assert session.outbound.empty()

    await session.handle_client({"type": "control", "action": "unmute"})
    unmuted = _drain(session)
    unmuted_state = next(event for event in unmuted if event.type == "state")
    assert unmuted_state.state["muted"] is False
    assert unmuted_state.state["paused"] is False
    assert session._closed is False
    session.close()


async def test_websocket_heartbeat_renews_an_expiring_live_lease(
    client: AsyncClient, db_session
) -> None:
    from uuid import UUID

    from app.auth import ActorContext
    from app.models import VoiceSession
    from app.utils.text import utcnow
    from app.voice.live.transport import bind_live_session

    reset_live_registry()
    await grant_voice_consent(client)
    opened = await client.post("/v1/voice/live/open", json={"device_id": "lease-mac"})
    assert opened.status_code == 201, opened.text
    session_id = UUID(opened.json()["session_id"])
    row = await db_session.get(VoiceSession, session_id)
    assert row is not None
    row.expires_at = utcnow() - timedelta(seconds=1)
    await db_session.commit()

    live = await bind_live_session(
        session_id=session_id,
        ctx=ActorContext(actor="master", is_master=True),
    )
    assert live.on_heartbeat is not None
    await live.on_heartbeat()
    await db_session.refresh(row)
    assert row.expires_at is not None
    from app.voice.lifecycle import idle_lock_expired

    assert not idle_lock_expired(row.expires_at)
    live.close()


async def test_capability_sheet_reaches_live_voice_with_enabled_setup_and_refused_states(
    db_session,
) -> None:
    from app.ev.protocols import capability_reply

    reset_live_registry()
    payload = await capability_reply(db_session, include_refused=True)
    protocols = payload["protocols"]
    assert protocols
    assert {item["status"] for item in protocols} & {"enabled"}
    assert {item["status"] for item in protocols} & {"refused"}
    assert all({"key", "title", "status", "detail"} <= set(item) for item in protocols)

    session = LiveSession(
        session_id="cap-real-1",
        capability_reply=lambda **_: payload,
        backchannel_enabled=False,
    )
    await session.handle_client({"type": "text", "text": "what can you do", "commit": True})
    events = _drain(session)
    hud = next(event for event in events if isinstance(event, HudEvent))
    assert hud.kind == "protocols"
    assert hud.card["items"]
    session.close()


async def test_capability_manifest_includes_runtime_context_for_voice(db_session) -> None:
    """The live model needs more than the static protocol-sheet tour."""

    from app.ev.protocols import capability_reply
    from app.voice.live.layer import build_live_capability_manifest

    payload = await capability_reply(db_session, include_refused=True)
    manifest = build_live_capability_manifest(
        payload,
        device_id="mac-manifest",
        tts_device_id="iphone-16-pro",
        provider="pipeline",
    )
    assert {
        "schema_version",
        "enabled",
        "protocols",
        "missing_permissions",
        "current_devices",
        "active_providers",
        "required_confirmation",
        "fallbacks",
        "unavailable",
    } <= set(manifest)
    assert manifest["current_devices"] == ["mac-manifest", "iphone-16-pro"]
    assert manifest["active_providers"]["realtime"] == "pipeline"

    session = LiveSession(
        session_id="cap-manifest-1",
        device_id="mac-manifest",
        tts_device_id="iphone-16-pro",
        capability_manifest=manifest,
        backchannel_enabled=False,
    )
    ready = session.ready_event().as_dict()
    assert ready["config"]["capability_manifest"] == manifest
    session.close()


def test_live_capability_manifest_preserves_runtime_projection() -> None:
    from app.voice.live.layer import build_live_capability_manifest

    runtime_manifest = {"schema_version": "ev.capability-manifest.v1", "actor": "voice"}
    projection = [{"type": "function", "name": "calculate"}]
    payload = {
        "protocols": [],
        "runtime_manifest": runtime_manifest,
        "live_tool_projection": projection,
        "approved_tools": ["calculate"],
        "executable_tools": ["calculate"],
        "current_device": {"id": "device-1"},
        "current_provider": "openai",
        "missing_setup": [{"name": "send_message", "availability": "not_connected"}],
        "requires_confirmation": [{"name": "place_call"}],
    }

    manifest = build_live_capability_manifest(payload, provider="openai")

    assert manifest["runtime_manifest"] == runtime_manifest
    assert manifest["live_tool_projection"] == projection
    assert manifest["approved_tools"] == ["calculate"]
    assert manifest["executable_tools"] == ["calculate"]
    assert manifest["current_device"] == {"id": "device-1"}
    assert manifest["current_provider"] == "openai"
    assert manifest["missing_setup"] == payload["missing_setup"]
    assert manifest["requires_confirmation"] == payload["requires_confirmation"]


async def test_iphone_se_is_tts_fallback_when_primary_iphone_is_unreachable(db_session) -> None:
    from app.ev.fleet import tts_playback_device
    from app.models import Device
    from app.utils.text import utcnow

    now = utcnow()
    primary = Device(
        name="iPhone 16 Pro",
        device_type="phone",
        capabilities=["voice", "camera"],
        last_seen_at=now - timedelta(seconds=3600),
    )
    fallback = Device(
        name="iPhone SE 2020",
        device_type="phone",
        capabilities=["voice", "notifications"],
        last_seen_at=now,
    )
    db_session.add_all([primary, fallback])
    await db_session.commit()
    picked = await tts_playback_device(db_session, now=now)
    assert picked is not None
    assert picked.name == "iPhone SE 2020"


def test_client_long_mute_and_audio_engine_recovery_contracts_are_wired() -> None:
    repo = Path(__file__).resolve().parents[2]
    mac = (repo / "macos/Sources/EV/LiveConversation.swift").read_text()
    ios = (repo / "ios/EVClient/Sources/EVClient/LiveVoiceCoordinator.swift").read_text()
    microphone = (repo / "ios/EVClient/Sources/EVClient/LiveVoice.swift").read_text()

    for source in (mac, ios):
        assert "timeIntervalSince($0) >= 20" in source
        assert "tearDownChannel()" in source
        assert "start(client:" in source or "start()" in source
    assert "nsCode(error) == -10867" in microphone
    assert "try startOnce(enqueue: enqueue, configure: nil)" in microphone
    assert "AVAudioSafe.start(engine)" in microphone
    assert "AVAudioSafe.stop(engine)" in microphone


def test_camera_state_display_contract_is_wired_beside_live_mute_controls() -> None:
    repo = Path(__file__).resolve().parents[2]
    mac_ui = (repo / "macos/Sources/EV/MenuBarView.swift").read_text()
    ios_ui = (repo / "ios/EVClient/Sources/EVUI/Views.swift").read_text()
    models = (repo / "ios/EVClient/Sources/EVClient/Models.swift").read_text()
    camera_states = {"off", "paused", "active", "denied", "unavailable", "error"}

    assert camera_states <= {token for token in camera_states if token in models.lower()}
    assert "cameraState" in mac_ui
    assert "Button(cameraButtonTitle)" in mac_ui
    assert "cameraState" in ios_ui
    assert "toggleCamera()" in ios_ui


def test_failure_language_and_personality_are_direct_and_honest() -> None:
    from app.ev.personality import identity_block
    from app.voice.live.grok_voice import grok_voice_instructions, openai_realtime_instructions

    camera_failure = spoken_hardware_failure("camera")
    assert "camera" in camera_failure.lower()
    assert any(word in camera_failure.lower() for word in ("denied", "unavailable"))
    assert "never a fake success" in identity_block("EVIE", "a direct assistant").lower()
    assert "short sentences" in grok_voice_instructions().lower()
    assert "action over essay" in grok_voice_instructions().lower()
    openai = openai_realtime_instructions().lower()
    assert "never present as chatgpt" in openai
    assert "do not use tools" not in openai
    assert "available ev function" in openai


async def test_camera_request_is_target_bound_and_never_claims_activation() -> None:
    reset_live_registry()
    session = LiveSession(session_id="camera-contract-1", device_id="mac", backchannel_enabled=False)
    ready = session.ready_event().as_dict()
    assert ready["config"]["camera_state"]["state"] == "off"
    assert ready["config"]["camera_state"]["visible"] is False

    await session.handle_client(
        {"type": "camera", "action": "active", "device_id": "mac"}
    )
    request = next(event for event in _drain(session) if event.type == "camera_request")
    assert request.action == "active"
    assert session.interaction_snapshot()["camera_state"]["state"] == "off"

    await session.publish_camera_state(
        {
            "state": "active",
            "visible": True,
            "device_id": "mac",
            "permission_state": "authorized",
            "explicit_request": True,
        }
    )
    state = next(event for event in _drain(session) if event.type == "camera_state")
    assert state.camera_state["state"] == "active"
    assert state.camera_state["visible"] is True
    session.close()


async def test_realtime_send_failure_enters_nonfatal_reconnect_path() -> None:
    reset_live_registry()
    fakes: list[_FakeRealtime] = []
    failures: list[ErrorEvent] = []

    class FailingRealtime(_FakeRealtime):
        fail_next_send = False

        async def send(self, data: str) -> None:
            if self.fail_next_send:
                self.fail_next_send = False
                raise ConnectionError("socket expired")
            await super().send(data)

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        fake = FailingRealtime()
        fakes.append(fake)
        return fake

    async def on_event(event):
        if isinstance(event, ErrorEvent):
            failures.append(event)

    bridge = GrokVoiceBridge(
        on_event=on_event,
        connect=connect,
        api_key="test",
        provider="openai",
        reconnect_delay_s=0.01,
    )
    await bridge.start()
    fakes[0].fail_next_send = True
    await bridge.append_pcm(b"\x00\x01" * 800)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and len(fakes) < 2:
        await asyncio.sleep(0.01)
    assert len(fakes) >= 2
    assert failures and all(event.fatal is False for event in failures)
    bridge.close()


async def test_pause_mute_boundary_discards_stale_asr_capture() -> None:
    session = LiveSession(session_id="capture-boundary", backchannel_enabled=False)
    assert session.asr_feed is None
    session._speech_active = True
    session._vad_hang_samples = 1600
    await session.handle_client({"type": "control", "action": "mute"})
    assert session._speech_active is False
    assert session._vad_hang_samples == 0
    assert session._vad is None
    await session.handle_client({"type": "control", "action": "unmute"})
    assert session._muted is False
    assert session._paused is False
    session.close()


async def test_provider_transcript_sleep_closes_live_session() -> None:
    session = LiveSession(session_id="provider-sleep", backchannel_enabled=False)
    from app.voice.live.events import FinalTranscriptEvent

    await session.emit(
        FinalTranscriptEvent(
            at_ms=session.now(), text="that's all", provider="openai-realtime"
        )
    )
    assert session._closed is True
    assert any(
        isinstance(event, ErrorEvent) and event.code == "session_ended"
        for event in _drain(session)
    )


def test_provider_manifest_instructions_are_truthful_and_dynamic() -> None:
    from app.voice.live.grok_voice import grok_session_update

    manifest = {
        "enabled": ["Personal memory"],
        "unavailable": [{"key": "web_search", "status": "needs_setup"}],
        "active_providers": {"realtime": "openai", "fallback": "pipeline"},
    }
    instructions = grok_session_update(
        provider="openai", capability_manifest=manifest
    )["session"]["instructions"]
    assert "CURRENT LIVE CAPABILITY MANIFEST" in instructions
    assert "web_search" in instructions
    assert "Never claim a tool completed" in instructions


async def test_failed_tool_result_is_not_rendered_as_evidence() -> None:
    session = LiveSession(session_id="failed-result", backchannel_enabled=False)
    await session.push_evidence(
        "place_call",
        {"ok": False, "error": "not_connected", "spoken": "Phone unavailable."},
    )
    events = _drain(session)
    hud = next(event for event in events if isinstance(event, HudEvent))
    assert hud.kind == "result"
    assert hud.card["meta"]["success"] is False
    session.close()
