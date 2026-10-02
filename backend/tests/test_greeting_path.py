"""Greeting / social / identity reflex path — instant, no model round trip."""

from __future__ import annotations

import pytest

from app.config import settings


def _kernel(monkeypatch) -> None:
    monkeypatch.setattr(settings, "cognitive_mode", "muse_kernel")
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
    monkeypatch.setattr(settings, "cognitive_mode", "legacy_mini")


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


def test_turn_needs_agent_splits_conversation_from_commands() -> None:
    from app.voice.live.session import LiveSession

    for text in (
        "text mom I'm late",
        "open safari",
        "search the web for the best coffee grinder",
        "find my notes file",
        "what's on my calendar today?",
        "remind me to call dad tomorrow",
    ):
        assert LiveSession._turn_needs_agent(text), text
    for text in (
        "hello",
        "how are you?",
        "give your introduction",
        "what can you do?",
        "tell me a joke",
        "explain why the sky is blue",
    ):
        assert not LiveSession._turn_needs_agent(text), text


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
    assert snapshot()["muse_turns"] == 0


@pytest.mark.asyncio
async def test_identity_turn_is_fast_and_stays_conversational(cognitive_isolation) -> None:
    from app.cognitive.kernel import handle_turn
    from app.cognitive.telemetry import snapshot

    result = await handle_turn(transcript="Give your introduction.")
    assert result.kind == "reflex:identity"
    assert "Evie" in result.spoken
    assert result.latency_ms < 2000, result.latency_ms
    assert snapshot()["muse_turns"] == 0


@pytest.mark.asyncio
async def test_good_morning_greeting_is_instant(cognitive_isolation) -> None:
    from app.cognitive.kernel import handle_turn

    result = await handle_turn(transcript="Good morning, Evie.")
    assert result.kind == "reflex:greeting"
    assert result.spoken == "Hello!"
    assert result.latency_ms < 200, result.latency_ms
