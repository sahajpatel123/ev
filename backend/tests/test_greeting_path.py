"""Greeting / social / identity reflex path — instant, no model round trip."""

from __future__ import annotations

import pytest

from app.config import settings


def _kernel(monkeypatch) -> None:
    monkeypatch.setattr(settings, "cognitive_mode", "mimo_kernel")
    monkeypatch.setattr(settings, "cognitive_role", "kernel")
    monkeypatch.setattr(settings, "laptop_files", False)


@pytest.fixture
def cognitive_isolation(monkeypatch, tmp_path):
    _kernel(monkeypatch)
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    from app.cognitive import telemetry
    from app.cognitive.session_store import reset_for_tests

    reset_for_tests()
    telemetry.reset_for_tests()
    yield
    reset_for_tests()
    telemetry.reset_for_tests()
    monkeypatch.setattr(settings, "cognitive_mode", "legacy_gemini")


def test_social_and_identity_match_including_asr_prefix() -> None:
    from app.cognitive.reflex import match_reflex

    for text in (
        "how are you?",
        "How are you doing?",
        "You, how are you?",
        "so, how's it going",
        "what's up",
    ):
        reflex = match_reflex(text, has_active_goal=False)
        assert reflex is not None, text
        assert reflex.kind == "social", text
        assert reflex.spoken

    for text in (
        "who are you?",
        "what's your name",
        "give your introduction",
        "give me your introduction",
        "Can you give me your introduction?",
        "Could you introduce yourself?",
        "please tell me about yourself",
        "Would you tell me your name?",
        "introduce yourself",
        "tell me about yourself",
        "what can you do?",
    ):
        reflex = match_reflex(text, has_active_goal=False)
        assert reflex is not None, text
        assert reflex.kind == "identity", text
        assert "Evie" in reflex.spoken

    assert match_reflex("thanks", has_active_goal=False).kind == "thanks"


def test_gemini_decides_via_delegate_task_not_local_routing() -> None:
    # Gemini-decides-all: EV does no local conversation/command split. The
    # split lives in the delegate_task contract Gemini reads.
    from app.cognitive.delegation import delegate_task_spec
    from app.voice.live.session import LiveSession

    assert not hasattr(LiveSession, "_turn_needs_agent")
    assert not hasattr(LiveSession, "_delegate_to_agent")
    spec = delegate_task_spec()
    assert spec["name"] == "delegate_task"
    blob = str(spec.get("description") or "").lower()
    assert "greetings" in blob and "small talk" in blob
    assert "actions" in blob or "action" in blob


def test_substantive_turns_never_match_a_reflex() -> None:
    from app.cognitive.reflex import match_reflex

    for text in (
        "Hello, is my order ready?",
        "hi there, what's the weather",
        "hey, remind me to call mom",
        "how are you going to fix this",
        "who are you sending that to?",
        "what can you do about the mail?",
        "Tell me about the EV project",
    ):
        assert match_reflex(text, has_active_goal=False) is None, text


@pytest.mark.asyncio
async def test_social_turn_is_fast_and_stays_conversational(cognitive_isolation) -> None:
    from app.cognitive.kernel import handle_turn
    from app.cognitive.telemetry import snapshot

    result = await handle_turn(transcript="You, how are you?")
    assert result.kind == "reflex:social"
    assert result.spoken
    assert result.latency_ms < 2000, result.latency_ms
    assert snapshot()["mimo_turns"] == 0


@pytest.mark.asyncio
async def test_identity_turn_is_fast_and_stays_conversational(cognitive_isolation) -> None:
    from app.cognitive.kernel import handle_turn
    from app.cognitive.telemetry import snapshot

    result = await handle_turn(transcript="Give your introduction.")
    assert result.kind == "reflex:identity"
    assert "Evie" in result.spoken
    assert result.latency_ms < 2000, result.latency_ms
    assert snapshot()["mimo_turns"] == 0


@pytest.mark.asyncio
async def test_good_morning_greeting_is_instant(cognitive_isolation) -> None:
    from app.cognitive.kernel import handle_turn

    result = await handle_turn(transcript="Good morning, Evie.")
    assert result.kind == "reflex:greeting"
    assert result.spoken == "Hello!"
    assert result.latency_ms < 200, result.latency_ms
