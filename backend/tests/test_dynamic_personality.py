"""Dynamic personality: adaptive length, moods, opinions, one voice everywhere.

v4: playful by default, focused on work, gentle when the owner is low —
and the "no personal preferences" dodge is banned in the contract and
stripped by the output filter, so every question gets a genuine answer.
"""

from __future__ import annotations

import pytest

from app.device_gateway.mobile_voice import MOBILE_CONVERSATION_CONTRACT
from app.ev import dynamic_personality as dp
from app.ev.interaction import build_strategy, strategy_block
from app.ev.personality import (
    PERSONALITY_CONTRACT_VERSION,
    SPEECH_STYLE_INSTRUCTIONS,
    identity_block,
)
from app.ev.turn import build_system_prompt, operator_instructions
from app.filter.output_filter import (
    apply_claim_actions,
    apply_persona_guardrails,
    audit_grounding,
    enforce_persona,
)


@pytest.fixture(autouse=True)
def _dynamic_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EV_PERSONALITY_DYNAMIC", "1")
    assert dp.dynamic_enabled()


def test_contract_version_is_dynamic() -> None:
    assert PERSONALITY_CONTRACT_VERSION == "dynamic.v4"
    assert "EV SPEECH CONTRACT" in SPEECH_STYLE_INSTRUCTIONS
    assert "length follows the need of the turn" in SPEECH_STYLE_INSTRUCTIONS
    assert "HAVE A MIND" in SPEECH_STYLE_INSTRUCTIONS
    assert "pick a side" in SPEECH_STYLE_INSTRUCTIONS
    assert "EVERY QUESTION GETS ANSWERED" in SPEECH_STYLE_INSTRUCTIONS
    assert "don't have personal preferences" in SPEECH_STYLE_INSTRUCTIONS


def test_trivia_stays_short_and_briefing_expands() -> None:
    assert dp.turn_shape("What is 2 plus 2?").shape in {"ack", "short", "standard"}
    briefing = dp.turn_shape("Brief me on the deployment in detail")
    assert briefing.shape == "briefing"
    decision = dp.turn_shape("Should I take the job?", intent="decision")
    assert decision.shape == "expanded"


def test_length_is_dynamic_not_static() -> None:
    shapes = {
        dp.turn_shape("Are you there?", intent="presence").shape,
        dp.turn_shape("Remind me at five", intent="life_action").shape,
        dp.turn_shape("hi").shape,
        dp.turn_shape("Explain how the retrieval pipeline works in detail").shape,
    }
    assert len(shapes) >= 3


def test_verbosity_slider_shifts_within_bounds() -> None:
    quiet = dp.turn_shape("What's up?", profile={"verbosity": 1})
    chatty = dp.turn_shape("What's up?", profile={"verbosity": 5})
    order = ["ack", "short", "standard", "expanded", "briefing"]
    assert order.index(chatty.shape) >= order.index(quiet.shape)


def test_owner_affect_shapes_guidance() -> None:
    tired = dp.turn_guidance("status?", emotion="tired")
    assert "low-energy" in tired
    stressed = dp.turn_guidance("status?", emotion="stressed")
    assert "acknowledge once" in stressed


def test_identity_carries_taste_but_contract_stays_last() -> None:
    for compact in (True, False):
        identity = identity_block("EVIE", "the owner's personal AI", compact=compact)
        assert "Your tastes" in identity
        assert "actual take" in identity
        assert identity.endswith(SPEECH_STYLE_INSTRUCTIONS)


def test_strategy_block_carries_dynamic_shape_before_contract() -> None:
    block = strategy_block(build_strategy("Explain the deployment tradeoffs"))
    assert "Turn shape:" in block
    assert block.rstrip().endswith(SPEECH_STYLE_INSTRUCTIONS)


def test_build_system_prompt_carries_dynamic_block_before_contract() -> None:
    prompt = build_system_prompt(
        identity="EV identity",
        who="E V",
        source="voice",
        working_on="WORKING ON: explain the tradeoffs in detail",
        context="",
        briefing=None,
        receipts=[],
    )
    assert "Turn shape:" in prompt
    assert prompt.endswith(SPEECH_STYLE_INSTRUCTIONS)


def test_operator_instructions_backward_compatible_without_message() -> None:
    text = operator_instructions(who="EVIE", source="voice")
    assert "Turn shape:" not in text
    assert text.rstrip().endswith(SPEECH_STYLE_INSTRUCTIONS)


def test_iphone_and_mac_share_one_personality() -> None:
    assert "shared dynamic personality" in MOBILE_CONVERSATION_CONTRACT
    assert "never a second personality" in MOBILE_CONVERSATION_CONTRACT


def test_opinion_lookup_is_honest_about_unknown_topics() -> None:
    assert dp.opinion_for("filler words in replies") is not None
    assert dp.opinion_for("cricket teams") is not None
    assert dp.opinion_for("astrology compatibility charts") is None


def test_stances_match_topics_and_carry_reasons() -> None:
    stance = dp.stance_for("Should we add another weekly sync meeting?")
    assert stance is not None
    assert stance.topic == "meetings"
    assert stance.reason
    assert 0.0 < stance.confidence <= 1.0
    assert dp.stance_for("What is the capital of France?") is None


def test_wants_opinion_detects_side_requests() -> None:
    assert dp.wants_opinion("What do you think — Postgres or SQLite?")
    assert dp.wants_opinion("Give me your honest opinion on this plan.")
    assert not dp.wants_opinion("What time is my meeting?")
    assert not dp.wants_opinion("Remind me at five.")


def test_opine_guidance_bans_the_dodge() -> None:
    with_stance = dp.opine_guidance("This design keeps growing new abstractions.")
    assert "simple beats clever" in with_stance
    assert "confidence" in with_stance
    assert "BANNED DODGE" in with_stance
    fallback = dp.opine_guidance("What do you think of this cricket team?")
    assert "reason from your tastes" in fallback
    assert "lean" in fallback


def test_smalltalk_block_is_warm_and_bounded() -> None:
    block = dp.smalltalk_block("Hey! Big news, I got the job!", emotion="excited")
    assert "at most one curious follow-up" in block
    assert "Never stack questions" in block
    assert "share the moment" in block
    low = dp.smalltalk_block("hey", emotion="tired", humor=0)
    assert "warmth first" in low
    assert "humor off" in low


def test_moods_playful_default_focused_on_work() -> None:
    assert dp.mood_for("hey what's up", mode="casual") == "playful"
    assert dp.mood_for("how are you", mode="social", emotion="excited") == "playful"
    assert dp.mood_for("fix the bug", mode="technical") == "focused_light"
    assert dp.mood_for("send it", intent="life_action") == "focused"
    assert dp.mood_for("help", mode="emergency", urgency=0.9) == "focused"
    assert dp.mood_for("ugh", emotion="frustrated") == "calm_direct"
    assert dp.mood_for("hey", emotion="tired") == "gentle"
    assert dp.mood_for("hey", mode="casual", humor=0) == "calm_direct"


def test_mood_block_carries_teasing_guardrails() -> None:
    block = dp.mood_block("hey", mode="casual")
    assert "Turn mood: playful" in block
    assert "trivial things only" in block
    assert "Never be mean" in block
    work = dp.mood_block("deploy it", mode="technical")
    assert "Turn mood: focused_light" in work
    assert "serious about the work" in work


def test_turn_guidance_carries_mood() -> None:
    guidance = dp.turn_guidance("hey, how's it going?")
    assert "Turn mood:" in guidance


def test_strategy_block_carries_opine_exactly_once() -> None:
    strategy = build_strategy("What do you think about all these meetings?")
    block = strategy_block(strategy, message="What do you think about all these meetings?")
    assert block.count("Have a mind:") == 1
    assert "most meetings should have been a message" in block
    assert block.rstrip().endswith(SPEECH_STYLE_INSTRUCTIONS)


def test_strategy_block_carries_smalltalk_for_casual() -> None:
    strategy = build_strategy("Hey, how are you?")
    block = strategy_block(strategy, message="Hey, how are you?")
    assert "Small talk: be someone, not something." in block
    assert block.rstrip().endswith(SPEECH_STYLE_INSTRUCTIONS)


def test_build_strategy_embeds_turn_shape() -> None:
    assert "Turn shape:" in build_strategy("hi").length_target
    assert "Turn shape: briefing" in build_strategy("Explain the tradeoffs in detail.").length_target


def test_opinion_sentences_survive_grounding_audit() -> None:
    draft = "I prefer Postgres for this. My take is the simple schema wins."
    claims, _ = audit_grounding(draft, [])
    assert claims == []
    final, edits = apply_claim_actions(draft, claims)
    assert edits == []
    assert "I prefer Postgres" in final


def test_biographical_inventions_still_removed() -> None:
    draft = "I visited Paris in 2019 and it was great."
    claims, _ = audit_grounding(draft, [])
    assert any(c.action == "remove" for c in claims)
    final, edits = apply_claim_actions(draft, claims)
    assert "Paris" not in final
    assert edits and edits[0]["type"] == "claim_removed"


def test_owner_history_claims_still_audited() -> None:
    draft = "You decided to move to Kyoto last March."
    claims, _ = audit_grounding(draft, [])
    assert any(c.action == "remove" for c in claims)


def test_no_opinion_dodge_is_stripped_keeping_remainder() -> None:
    draft = (
        "I don't have personal preferences about cricket. "
        "Many people love the strategy of test matches."
    )
    final, persona, flags = apply_persona_guardrails(draft)
    assert "don't have personal preferences" not in final
    assert "test matches" in final
    assert persona.get("opinion_dodge_stripped") == 1
    assert any(f.name == "opinion_dodge_stripped" for f in flags)


def test_pure_dodge_becomes_playful_invitation() -> None:
    final, persona, _ = apply_persona_guardrails(
        "As an AI, I don't have opinions on this."
    )
    assert "don't have opinions" not in final
    assert "argue the other side" in final
    assert persona.get("opinion_dodge_fallback") is True


def test_honest_boundaries_are_not_dodges() -> None:
    boundary = "I'm EV, an AI — I don't have human feelings or a body."
    final, persona, _ = apply_persona_guardrails(boundary)
    assert "human feelings" in final
    assert persona.get("opinion_dodge_stripped") is None
    memory = "I don't have that in memory, so I can't confirm it."
    final, _, _ = apply_persona_guardrails(memory)
    assert "in memory" in final


def test_word_caps_follow_turn_shape() -> None:
    briefing_strategy = build_strategy("Explain the deployment in detail.")
    long_draft = " ".join(["word"] * 200)
    final, persona, _ = enforce_persona(long_draft, briefing_strategy)
    assert not persona.get("length_trimmed")
    assert len(final.split()) == 200
    short_strategy = build_strategy("Are you there?")
    final, persona, _ = enforce_persona(long_draft, short_strategy)
    assert persona.get("length_trimmed") is True


def test_rollback_switch_restores_frozen_brevity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EV_PERSONALITY_DYNAMIC", "0")
    assert not dp.dynamic_enabled()
    assert dp.turn_shape("Explain everything in detail").shape == "short"
    identity = identity_block("EVIE", "the owner's personal AI", compact=True)
    assert "Your tastes" not in identity
    assert identity.endswith(SPEECH_STYLE_INSTRUCTIONS)
