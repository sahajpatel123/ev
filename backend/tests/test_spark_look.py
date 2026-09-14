"""Muse Spark 1.3 camera brain: first-try look vs recall vs chat."""

from __future__ import annotations

import pytest

from app.ev.spark_look import (
    decide_camera_action,
    fallback_camera_action,
    maybe_camera_utterance,
    should_spark_camera,
)

HOLDING = (
    "Look at the thing I'm holding in my hand. I want you to look at it "
    "and tell me more info about this item I'm holding"
)


def test_fallback_looks_on_first_try_hold_without_memorize() -> None:
    assert fallback_camera_action(HOLDING) == "look"
    assert fallback_camera_action("what am I holding") == "look"
    assert fallback_camera_action("memorize this") == "look"
    assert fallback_camera_action("what did I ask you to remember") == "recall"
    assert fallback_camera_action("look this up on the web") is None
    assert fallback_camera_action("I'm holding a meeting at three") is None
    assert fallback_camera_action("how's the weather") is None


def test_hold_is_a_spark_camera_job_and_a_first_try_look() -> None:
    assert fallback_camera_action(HOLDING) == "look"
    assert should_spark_camera(HOLDING) is True
    assert maybe_camera_utterance(HOLDING) is False
    assert (
        fallback_camera_action("point your camera at whatever is in front of you")
        == "look"
    )
    assert maybe_camera_utterance(
        "describe whatever is sitting in front of the camera"
    )
    assert maybe_camera_utterance("look this up") is False
    assert maybe_camera_utterance("how's the weather") is False


@pytest.mark.asyncio
async def test_spark_is_asked_on_hold_and_fallback_if_dark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = {"n": 0}

    async def dark(*_args, **_kwargs):
        called["n"] += 1
        return None

    monkeypatch.setattr("app.ev.spark_look._spark_decide", dark)
    assert await decide_camera_action(HOLDING) == "look"
    assert called["n"] == 1


@pytest.mark.asyncio
async def test_spark_wins_on_hold_when_it_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_spark(_utterance: str) -> str:
        return "look"

    monkeypatch.setattr("app.ev.spark_look._spark_decide", fake_spark)
    assert await decide_camera_action(HOLDING) == "look"


@pytest.mark.asyncio
async def test_spark_look_classifies_ambiguous_camera_ask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_spark(_utterance: str) -> str:
        return "look"

    monkeypatch.setattr("app.ev.spark_look._spark_decide", fake_spark)
    monkeypatch.setattr(
        "app.ev.spark_look.maybe_camera_utterance",
        lambda _text: True,
    )
    monkeypatch.setattr("app.ev.spark_look.fallback_camera_action", lambda _text: None)
    assert await decide_camera_action("can you check out what's in front of you") == "look"
