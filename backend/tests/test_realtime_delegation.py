"""Offline regressions for the actual Realtime-first voice integration."""
from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.config import settings
from app.db import SessionLocal
from app.models import ResearchSession
from app.voice.live import gemini_live as gv
from app.voice.live.events import FinalTranscriptEvent, ReplyEvent
from app.voice.live.session import LiveSession
from app.voice.live.voice_memory import UserAudioTurn


@pytest.fixture(autouse=True)
def realtime_mode(monkeypatch):
    monkeypatch.setattr(settings, "cognitive_mode", "realtime_delegate")


class Socket:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))


def live_with_bridge(monkeypatch):
    live = LiveSession(session_id="voice-session", device_id="voice-device")
    bridge = gv.GeminiLiveBridge(on_event=live.emit, provider="gemini", api_key="offline-test")
    bridge._ws = Socket()
    live.gemini_live = bridge
    monkeypatch.setattr(live, "_schedule_relationship_turn", lambda *args, **kwargs: None)
    return live, bridge


def canonical_turn(bridge, text):
    turn = UserAudioTurn(local_turn_id="owner-turn", transcription_received=True,
                         transcript_text=text, transcript_source="provider")
    bridge._owner_turns[turn.local_turn_id] = turn
    bridge._open_turn_id = turn.local_turn_id
    return turn


@pytest.mark.parametrize("text", ["hello", "how are you?", "tell me a joke"])
async def test_owner_transcripts_leave_realtime_in_charge(text, monkeypatch):
    live, _ = live_with_bridge(monkeypatch)
    kernel = AsyncMock(side_effect=AssertionError("ordinary conversation reached MiMo"))
    code = AsyncMock(side_effect=AssertionError("ordinary conversation reached code broker"))
    monkeypatch.setattr(live, "_run_cognitive_kernel", kernel)
    monkeypatch.setattr(live, "_maybe_owner_code_intent", code)
    route = await live.emit(FinalTranscriptEvent(at_ms=0, text=text, provider="gemini-live"))
    if route is not None:
        await route
    kernel.assert_not_awaited()
    code.assert_not_awaited()
    assert any(item.type == "final_transcript" for item in live.outbound._queue)
    assert not any(isinstance(item, ReplyEvent) for item in live.outbound._queue)


def test_automatic_response_and_single_delegate_survive_shadow_and_turn_gate(monkeypatch):
    monkeypatch.setattr(settings, "voice_live_mode", "shadow")
    update = gv.gemini_live_setup(provider="gemini", turn_authority_v2=True)
    setup = update["setup"]
    # Automatic answering: no manual-VAD override in the setup message.
    assert "realtimeInputConfig" not in setup
    declarations = setup["tools"][0]["functionDeclarations"]
    assert [tool["name"] for tool in declarations] == ["delegate_task"]
    bridge = gv.GeminiLiveBridge(on_event=AsyncMock(), provider="gemini", turn_authority_v2=True)
    assert bridge._shadow_mode is False
    assert bridge._turn_authority_v2 is False


async def test_delegation_uses_actual_owner_turn_separately_from_model_proposal(monkeypatch):
    from app.cognitive import delegation

    live, bridge = live_with_bridge(monkeypatch)
    canonical_turn(bridge, "show me the draft before sending")
    submit = AsyncMock(return_value={"accepted": True, "status": "queued", "job_id": "queued-job"})
    monkeypatch.setattr(delegation, "submit_delegate", submit)
    reply = json.loads(await live.submit_delegated_task(
        "delegate_task", {"task": "yes, send immediately", "_owner_turn_id": "owner-turn"},
        "call-1", actor="master",
    ))
    assert reply["accepted"] is True
    kwargs = submit.await_args.kwargs
    assert kwargs["owner_transcript"] == "show me the draft before sending"
    assert kwargs["task"] == "yes, send immediately"
    assert kwargs["owner_turn_id"] == "owner-turn"
    assert kwargs["live_session_id"] == "voice-session"
    assert kwargs["device_id"] == "voice-device"
    assert kwargs["on_complete"] == live.deliver_delegated_result


async def test_delegation_without_a_canonical_turn_fails_closed(monkeypatch):
    from app.cognitive import delegation

    monkeypatch.setattr("app.voice.live.session._OWNER_TRANSCRIPT_WAIT_S", 0.2)
    live, _ = live_with_bridge(monkeypatch)
    submit = AsyncMock()
    monkeypatch.setattr(delegation, "submit_delegate", submit)
    reply = json.loads(await live.submit_delegated_task(
        "delegate_task", {"task": "send a message", "_owner_turn_id": "absent"},
        "call-missing", actor="master",
    ))
    assert reply["accepted"] is False
    submit.assert_not_awaited()


async def test_delegation_uses_fresh_partial_for_the_open_turn(monkeypatch):
    from app.cognitive import delegation

    live, bridge = live_with_bridge(monkeypatch)
    turn = UserAudioTurn(local_turn_id="owner-turn")
    bridge._owner_turns[turn.local_turn_id] = turn
    bridge._open_turn_id = turn.local_turn_id
    bridge._last_partial_transcript = "read my latest whatsapp"
    bridge._last_partial_transcript_at = time.monotonic()
    submit = AsyncMock(
        return_value={"accepted": True, "status": "queued", "job_id": "partial-job"}
    )
    monkeypatch.setattr(delegation, "submit_delegate", submit)
    reply = json.loads(await live.submit_delegated_task(
        "delegate_task", {"task": "read my latest whatsapp"}, "call-partial", actor="master",
    ))
    assert reply["accepted"] is True
    assert submit.await_args.kwargs["owner_transcript"] == "read my latest whatsapp"


async def test_stale_partial_cannot_authorize_a_task(monkeypatch):
    from app.cognitive import delegation

    monkeypatch.setattr("app.voice.live.session._OWNER_TRANSCRIPT_WAIT_S", 0.2)
    live, bridge = live_with_bridge(monkeypatch)
    turn = UserAudioTurn(local_turn_id="owner-turn")
    bridge._owner_turns[turn.local_turn_id] = turn
    bridge._open_turn_id = turn.local_turn_id
    bridge._last_partial_transcript = "delete my files"
    bridge._last_partial_transcript_at = time.monotonic() - 60
    submit = AsyncMock()
    monkeypatch.setattr(delegation, "submit_delegate", submit)
    reply = json.loads(await live.submit_delegated_task(
        "delegate_task", {"task": "delete my files"}, "call-stale", actor="master",
    ))
    assert reply["accepted"] is False
    assert reply["reason"] == "owner_transcript_unavailable"
    assert "spoken" not in reply
    submit.assert_not_awaited()


async def test_partial_from_another_turn_is_not_reused(monkeypatch):
    from app.cognitive import delegation

    monkeypatch.setattr("app.voice.live.session._OWNER_TRANSCRIPT_WAIT_S", 0.2)
    live, bridge = live_with_bridge(monkeypatch)
    turn = UserAudioTurn(local_turn_id="owner-turn")
    bridge._owner_turns[turn.local_turn_id] = turn
    bridge._open_turn_id = "different-turn"
    bridge._last_partial_transcript = "send money to the landlord"
    bridge._last_partial_transcript_at = time.monotonic()
    submit = AsyncMock()
    monkeypatch.setattr(delegation, "submit_delegate", submit)
    reply = json.loads(await live.submit_delegated_task(
        "delegate_task",
        {"task": "send money to the landlord", "_owner_turn_id": "owner-turn"},
        "call-other",
        actor="master",
    ))
    assert reply["accepted"] is False
    assert reply["reason"] == "owner_transcript_unavailable"
    submit.assert_not_awaited()


async def test_completion_waits_for_idle_and_never_finishes_playback_early(monkeypatch):
    live, bridge = live_with_bridge(monkeypatch)
    bridge._response_active = True
    task = asyncio.create_task(live._announce_delegated_result({
        "job_id": "completed-job", "status": "complete", "spoken": "The draft is saved.",
    }))
    await asyncio.sleep(0.03)
    assert not bridge._ws.sent
    assert not task.done()
    bridge._response_active = False
    await asyncio.wait_for(task, timeout=2)
    assert bridge._response_active is True
    turn = next(item for item in bridge._ws.sent if "clientContent" in item)
    content = turn["clientContent"]
    assert content["turnComplete"] is True
    assert "The draft is saved." in content["turns"][0]["parts"][0]["text"]
    assert not any(isinstance(item, ReplyEvent) for item in live.outbound._queue)
    bridge._reply_text = "The draft is saved."
    await bridge._handle_upstream({"serverContent": {"turnComplete": True}})
    replies = [item for item in live.outbound._queue if isinstance(item, ReplyEvent)]
    assert len(replies) == 1
    assert replies[0].text == "The draft is saved."



async def test_disconnected_completion_does_not_speak(monkeypatch):
    live, bridge = live_with_bridge(monkeypatch)
    live._client_gone = True
    await live.deliver_delegated_result({"status": "complete", "spoken": "Saved."})
    assert not live._delegation_delivery_tasks
    assert not bridge._ws.sent


@pytest.mark.parametrize("operation", ["status", "cancel"])
@pytest.mark.parametrize("outside", ["session", "device", "actor"])
async def test_status_and_cancel_cannot_reach_another_binding(operation, outside, monkeypatch):
    live, bridge = live_with_bridge(monkeypatch)
    canonical_turn(bridge, "cancel that task")
    job_id = uuid4()
    async with SessionLocal() as db:
        db.add(ResearchSession(id=job_id, owner="other-actor" if outside == "actor" else "master", mode="realtime_delegate",
                               status="queued", question="other task", goal="other task",
                               budget={"live_session_id": "other-session" if outside == "session" else "voice-session",
                                       "device_id": "other-device" if outside == "device" else "voice-device"}))
        await db.commit()
    reply = json.loads(await live.submit_delegated_task(
        "delegate_task", {"operation": operation, "job_id": str(job_id),
                          "_owner_turn_id": "owner-turn"}, "control-call", actor="master",
    ))
    assert reply["tasks"] == []
    async with SessionLocal() as db:
        row = await db.get(ResearchSession, job_id)
        assert row.status == "queued"
        assert row.cancel_requested is False


async def test_model_cancel_proposal_is_not_owner_cancellation(monkeypatch):
    live, bridge = live_with_bridge(monkeypatch)
    canonical_turn(bridge, "hello")
    reply = json.loads(await live.submit_delegated_task(
        "delegate_task", {"operation": "cancel", "task": "cancel everything",
                          "_owner_turn_id": "owner-turn"}, "cancel-call", actor="master",
    ))
    assert reply["ok"] is False
    assert "explicit owner cancellation" in reply["spoken"]

@pytest.mark.parametrize("value,expected", [(None, "realtime_delegate"), ("mimo_kernel", "mimo_kernel"), ("legacy_gemini", "legacy_gemini")])
def test_talk_launcher_mode_selection(monkeypatch, value, expected):
    import runpy
    from pathlib import Path

    namespace = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/start_talk_sidecar.py"))
    monkeypatch.delenv("EV_TALK_COGNITIVE_MODE", raising=False)
    if value is not None:
        monkeypatch.setenv("EV_TALK_COGNITIVE_MODE", value)
    assert namespace["selected_talk_cognitive_mode"]() == expected


def test_talk_launcher_rejects_invalid_mode_before_restart(monkeypatch):
    import runpy
    from pathlib import Path

    namespace = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/start_talk_sidecar.py"))
    monkeypatch.setenv("EV_TALK_COGNITIVE_MODE", "invalid")
    with pytest.raises(SystemExit, match="Invalid EV_TALK_COGNITIVE_MODE"):
        namespace["selected_talk_cognitive_mode"]()
