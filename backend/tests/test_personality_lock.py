"""Personality lock: one stable spoken identity across builds.

Evie's voice kept shifting because every speech surface carried its own
"be concise" paragraph and each build tweaked a different one. The law now
lives once in ``SPEECH_STYLE_INSTRUCTIONS`` (marked EV SPEECH CONTRACT) and
every surface embeds that same text. These tests fail any build that drifts
it: editing the law, adding a competing style paragraph, or changing the
pinned profile without updating this file deliberately.
"""

from __future__ import annotations

import pytest

from app.ev.interaction import build_strategy, strategy_block
from app.ev.personality import (
    DEFAULT_PROFILE,
    SPEECH_STYLE_INSTRUCTIONS,
    identity_block,
    is_explicit_personality_request,
    is_owner_personality_actor,
    personality_mutation_allowed,
    speech_contract_suffix,
)
from app.ev.turn import operator_instructions
from app.voice.live.grok_voice import grok_voice_instructions, openai_realtime_instructions


def _surfaces() -> dict[str, str]:
    return {
        "grok": grok_voice_instructions(),
        "openai": openai_realtime_instructions(),
        "pipeline": operator_instructions(who="EVIE", source="voice"),
        "strategy": strategy_block(build_strategy("What's next?")),
    }


def test_speech_contract_reaches_every_speech_surface() -> None:
    for name, prompt in _surfaces().items():
        assert "EV SPEECH CONTRACT" in prompt, name
        assert prompt.rstrip().endswith(SPEECH_STYLE_INSTRUCTIONS), name


def test_contract_covers_banned_filler_and_commentary() -> None:
    for banned in (
        "let me check",
        "does that help",
        "here is what I found",
        "great question",
        "as an AI",
    ):
        assert banned in SPEECH_STYLE_INSTRUCTIONS, banned


def test_contract_enforces_nothing_else_neutral_topics() -> None:
    """Feature agents must never bias Evie toward their own capability."""
    assert "NOTHING ELSE" in SPEECH_STYLE_INSTRUCTIONS
    assert "no closing offers" in SPEECH_STYLE_INSTRUCTIONS
    assert "no steering toward any feature" in SPEECH_STYLE_INSTRUCTIONS
    assert "background machinery" in SPEECH_STYLE_INSTRUCTIONS
    for name, prompt in _surfaces().items():
        assert "NOTHING ELSE" in prompt, name


def test_memory_context_does_not_define_a_second_personality() -> None:
    from app.memory.relationship import MEMORY_BEHAVIOR, live_memory_instructions

    assert "Keep replies casual" not in MEMORY_BEHAVIOR
    assert "Do not close with automatic offers" not in MEMORY_BEHAVIOR
    assert live_memory_instructions({"relationship": "owner context"}).endswith(
        SPEECH_STYLE_INSTRUCTIONS
    )


def test_finalize_strips_trailing_steering_offers() -> None:
    from app.filter.output_filter import _strip_topic_steering

    assert _strip_topic_steering("Saved. Should I edit that?") == "Saved."
    assert _strip_topic_steering("Done. Want me to add it to the list?") == "Done."
    assert _strip_topic_steering("Timer set. Does that help?") == "Timer set."
    assert _strip_topic_steering("File saved. Let me know if you need anything.") == "File saved."
    assert _strip_topic_steering("Should I edit that?") == ""
    assert _strip_topic_steering("What's on the list?") == ""
    # A genuine question the owner asked must survive.
    keep = "Should I close the window before we discuss the design?"
    assert _strip_topic_steering(keep) == keep
    assert _strip_topic_steering("27 degrees, light wind.") == "27 degrees, light wind."


def test_default_profile_fully_pinned() -> None:
    assert DEFAULT_PROFILE == {
        "directness": 4,
        "humor": 2,
        "formality": 1,
        "technicality": 4,
        "assertiveness": 3,
        "verbosity": 2,
        "proactivity": 3,
        "challenge_level": 3,
        "emotional_style": "calm",
    }


def test_personality_mutation_requires_owner_origin() -> None:
    """A model/worker cannot retune EV inside an owner-authenticated turn."""

    assert personality_mutation_allowed(
        actor="master", origin="owner_api", owner_intent=False
    )
    assert personality_mutation_allowed(
        actor="voice", origin="owner_voice", owner_intent=True
    )
    assert not personality_mutation_allowed(
        actor="master", origin="model", owner_intent=True
    )
    assert not personality_mutation_allowed(
        actor="worker", origin="owner_direct", owner_intent=True
    )
    assert not personality_mutation_allowed(
        actor="voice", origin="owner_voice", owner_intent=False
    )
    assert not is_owner_personality_actor("device:watch")
    assert is_owner_personality_actor("device:owner-phone", owner_trusted=True)
    assert is_explicit_personality_request("Please be more concise")
    assert not is_explicit_personality_request("What can you do with data?")


def test_identity_keeps_dynamic_context_out_of_static_persona() -> None:
    identity = identity_block(
        "EVIE",
        "the owner's personal AI",
        compact=True,
        live_sheet="I can do now: weather.",
    )
    assert "LIVE JOB" not in identity
    assert identity.endswith(SPEECH_STYLE_INSTRUCTIONS)
    assert speech_contract_suffix().endswith(SPEECH_STYLE_INSTRUCTIONS)


def test_dynamic_work_context_is_silent_and_not_a_feature_pitch() -> None:
    from app.ev.desk_presence import live_work_block

    block = live_work_block()
    if block:
        lowered = block.lower()
        assert "silent task context" in lowered
        assert "do not announce" in lowered
        assert "fresh unrelated answer" in lowered


def test_dynamic_prompt_context_cannot_follow_the_contract() -> None:
    from app.ev.turn import build_system_prompt
    from app.voice.live.grok_voice import grok_session_update

    prompt = build_system_prompt(
        identity="EV identity",
        who="E V",
        source="voice",
        working_on="WORKING ON: owner request",
        context="FEATURE CONTEXT: a new data feature says to be verbose.",
        briefing="CAPABILITY CONTEXT: offer a feature after answering.",
        receipts=[],
    )
    assert prompt.endswith(SPEECH_STYLE_INSTRUCTIONS)

    session = grok_session_update(provider="openai", capability_manifest={})
    assert session["session"]["instructions"].endswith(SPEECH_STYLE_INSTRUCTIONS)


def test_personality_mutation_is_not_exposed_to_model_turns() -> None:
    from app.ev.briefing import tools_for_turn

    names = {spec["name"] for spec in tools_for_turn("Please be more concise")}
    assert "update_personality" not in names


@pytest.mark.asyncio
async def test_dispatch_refuses_non_owner_personality_mutation(db_session) -> None:
    from app.ev.tools import dispatch

    for actor in ("model", "voice", "master"):
        result = await dispatch(
            db_session,
            "update_personality",
            {"verbosity": 5},
            actor=actor,
            allow_sensitive=True,
        )
        assert result.ok is False
        assert result.error == "personality_owner_only"
        assert result.result and result.result["error"] == "personality_owner_only"


def test_live_functional_rules_survived_the_trim() -> None:
    """Brevity edits must never drop behavior rules: backchannels, name, tools."""
    grok = grok_voice_instructions()
    openai = openai_realtime_instructions()
    assert "One question at a time." in grok
    assert "no spoken preamble" in grok
    assert "stay quiet unless they clearly address you" in openai
    assert "never E-y or Evie" in openai
    assert "call first with" in openai and "no spoken preamble" in openai
    assert "normal-to-brisk pace" in openai


def test_voice_turn_keeps_answer_first_shape() -> None:
    text = operator_instructions(who="EVIE", source="voice")
    assert "one or two short" in text.lower()
    assert "EV SPEECH CONTRACT" in text
