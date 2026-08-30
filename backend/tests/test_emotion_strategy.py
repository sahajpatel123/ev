"""Owner emotion → strategy → speech style on the shipped path."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.ev.interaction import (
    build_strategy,
    detect_emotion,
    detect_intent,
    strategy_block,
)
from app.voice.tts import speech_style_from_strategy
from tests.test_voice_lifecycle import grant_voice_consent

FEELING_TEXTS = {
    "stressed": "I'm completely overwhelmed and stressed about today",
    "frustrated": "This is so frustrating, I'm sick of this",
    "tired": "I'm exhausted and have no energy left",
    "sad": "I feel sad and lonely tonight",
    "excited": "I'm so excited, I can't wait for this",
}

NEUTRAL_CALENDAR = "what's next on my calendar"
TIRED_CALENDAR = "I'm exhausted, what's next on my calendar"


def test_detect_emotion_distinguishes_owner_feelings() -> None:
    labels = {name: detect_emotion(text) for name, text in FEELING_TEXTS.items()}
    labels["neutral"] = detect_emotion(NEUTRAL_CALENDAR)
    assert labels["neutral"] == "neutral"
    assert labels["stressed"] == "stressed"
    assert labels["frustrated"] == "frustrated"
    assert labels["tired"] == "tired"
    assert labels["sad"] == "sad"
    assert labels["excited"] == "excited"
    assert len(set(labels.values())) > 1
    # Catch-all: "the server is down" must not become sad.
    assert detect_emotion("the production server is down") == "neutral"


def test_build_strategy_carries_distinct_emotional_states() -> None:
    states = [build_strategy(text).emotional_state for text in FEELING_TEXTS.values()]
    states.append(build_strategy(NEUTRAL_CALENDAR).emotional_state)
    assert len(set(states)) >= 4
    assert "neutral" in states
    assert build_strategy(TIRED_CALENDAR).emotional_state == "tired"


def test_speech_style_differs_for_feeling_vs_neutral_same_task() -> None:
    feeling = build_strategy(TIRED_CALENDAR)
    neutral = build_strategy(NEUTRAL_CALENDAR)
    feel_style = speech_style_from_strategy(feeling)
    neut_style = speech_style_from_strategy(neutral)
    assert feeling.emotional_state == "tired"
    assert neutral.emotional_state == "neutral"
    assert (
        feel_style.warmth != neut_style.warmth
        or feel_style.urgency != neut_style.urgency
        or feel_style.mode != neut_style.mode
    )
    assert feel_style.warmth > neut_style.warmth
    frustrated = speech_style_from_strategy(
        build_strategy("this is so frustrating, what's next on my calendar")
    )
    assert frustrated.warmth < neut_style.warmth or frustrated.urgency > neut_style.urgency


def test_feeling_plus_command_keeps_task_intent() -> None:
    strategy = build_strategy(TIRED_CALENDAR)
    assert strategy.intent in {"question", "command", "general"}
    assert strategy.intent != "venting"
    block = strategy_block(strategy)
    assert "tired" in block.lower()
    assert "asked task" in block.lower()


def test_venting_does_not_swallow_a_calendar_ask() -> None:
    assert detect_intent("ugh I'm tired, what's next on my calendar") == "question"


def test_presence_check_intent_asks_for_a_hearing_reply() -> None:
    strategy = build_strategy("Evie can you hear me?")
    assert strategy.intent == "presence"
    block = strategy_block(strategy)
    assert "hear" in block.lower()
    assert detect_intent("evie can you check the time") != "presence"


@pytest.mark.asyncio
async def test_feeling_plus_command_stream_still_answers_the_task(
    client: AsyncClient,
) -> None:
    """Shipped voice stream: tired + calendar still addresses the calendar."""

    await grant_voice_consent(client)
    wake = await client.post(
        "/v1/voice/wake",
        json={"device_id": "mac-emotion-cal", "push_to_talk": True},
    )
    assert wake.status_code == 201, wake.text
    resp = await client.post(
        "/v1/voice/utterance/stream",
        json={
            "session_id": wake.json()["session_id"],
            "text": TIRED_CALENDAR,
            "push_to_talk": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "event: reply" in body
    assert "calendar" in body.lower() or "what's next" in body.lower()
    comfort_only = {
        "i hear you",
        "i'm sorry",
        "that sounds hard",
        "that sounds tough",
    }
    reply_text = ""
    for block in body.split("\n\n"):
        if "event: reply" in block or block.startswith("data:"):
            pass
    # Pull the reply field from the SSE payload without re-implementing chat.
    import json

    current = ""
    for line in body.splitlines():
        if line.startswith("event:"):
            current = line[6:].strip()
        elif line.startswith("data:") and current == "reply":
            payload = json.loads(line[5:].strip())
            reply_text = str(payload.get("reply") or "")
            break
    assert reply_text
    assert reply_text.strip().lower() not in comfort_only
    assert "calendar" in reply_text.lower() or "what's next" in reply_text.lower()
