"""Continuity detection: fresh questions must not inherit old-thread context."""

from __future__ import annotations

import pytest

from app.ev.continuity import is_continuation, is_self_contained


@pytest.mark.parametrize(
    "message",
    [
        "what's the weather in Gujarat?",
        "can you check what the weather outside is?",
        "how is the stock market doing today?",
        "what time is it?",
        "is it raining in Ahmedabad?",
        "tell me about the Indian economy",
        "what are you doing right now?",
    ],
)
def test_fresh_questions_are_self_contained(message: str) -> None:
    assert is_self_contained(message), message
    assert not is_continuation(message), message


@pytest.mark.parametrize(
    "message",
    [
        "as I asked before about the markets",
        "you said the market was up yesterday",
        "about that market thread",
        "and what about the weather you mentioned?",
        "I think I figured out how I want that memory thing to work.",
        "I finally fixed that camera problem.",
        "which 8 do not repeat at the end of its answer",
        "continue the earlier conversation",
        "going back to the stock market question",
        "what about it?",
        "and also the audio",
        "And tomorrow",
        "make it tomorrow morning",
        "Also remind me to buy milk",
    ],
)
def test_continuation_messages_keep_context(message: str) -> None:
    assert is_continuation(message), message
    assert not is_self_contained(message), message


def test_empty_message_is_self_contained() -> None:
    assert is_self_contained(None)
    assert is_self_contained("")
    assert is_self_contained("   ")
