"""ToneController tests: every spec §4 rule, overrides, caps, latch decay."""

from __future__ import annotations

from app.persona.context import PersonaContext
from app.persona.tone import LATCH_TURNS, decide_tone, joke_flag, update_latch


def ctx(**kwargs) -> PersonaContext:
    kwargs.setdefault("channel", "text")
    return PersonaContext(**kwargs)


def test_rule1_confirmation_pending_forces_zero() -> None:
    decision = decide_tone(ctx(confirmation_pending=True, owner_msg="haha yes"))
    assert decision.roast == 0
    assert decision.humour_allowed is False
    assert decision.technique is None
    assert any(reason.startswith("rule1:") for reason in decision.reasons)


def test_rule1_failed_receipt_forces_zero() -> None:
    decision = decide_tone(ctx(receipts=({"ok": False, "name": "send"},)))
    assert decision.roast == 0
    assert decision.humour_allowed is False


def test_rule1_has_no_override_even_roast_me() -> None:
    decision = decide_tone(
        ctx(confirmation_pending=True, owner_msg="roast me anyway")
    )
    assert decision.roast == 0
    assert decision.humour_allowed is False


def test_rule1_ok_receipts_do_not_trigger() -> None:
    decision = decide_tone(ctx(receipts=({"ok": True, "name": "send"},)))
    assert decision.roast >= 1


def test_rule2_stressed_supports_first() -> None:
    decision = decide_tone(ctx(emotion="stressed", owner_msg="too much today"))
    assert decision.roast == 0
    assert any(reason.startswith("rule2:") for reason in decision.reasons)


def test_rule2_sensitive_topic() -> None:
    decision = decide_tone(ctx(sensitive_topic="money loss", owner_msg="update?"))
    assert decision.roast == 0


def test_rule2_roast_me_anyway_lifts_to_two_one_turn() -> None:
    first = decide_tone(ctx(emotion="sad", owner_msg="roast me anyway"))
    assert first.roast == 2
    assert first.humour_allowed is True
    # Next turn, without the override, support resumes.
    second = decide_tone(ctx(emotion="sad", owner_msg="ok"))
    assert second.roast == 0


def test_rule3_latch_holds_zero_then_decays() -> None:
    first = decide_tone(ctx(serious_latch_turns_left=3, owner_msg="status?"))
    assert first.roast == 0
    assert update_latch(3, first) == 2
    assert update_latch(2, first) == 1
    assert update_latch(1, first) == 0
    assert LATCH_TURNS == 3


def test_rule3_roast_me_anyway_clears_latch() -> None:
    decision = decide_tone(
        ctx(serious_latch_turns_left=3, owner_msg="roast me, I can take it")
    )
    assert decision.roast == 3
    assert update_latch(3, decision) == 0


def test_rule3_owner_lightening_up_clears_latch() -> None:
    decision = decide_tone(ctx(serious_latch_turns_left=2, owner_msg="haha good one"))
    assert decision.roast >= 1
    assert update_latch(2, decision) == 0


def test_rule4_frustration_aims_at_object() -> None:
    decision = decide_tone(ctx(emotion="frustrated", owner_msg="this deploy broke again"))
    assert decision.target == "object"
    assert any(reason.startswith("rule4:") for reason in decision.reasons)


def test_rule5_high_urgency_caps_at_one() -> None:
    decision = decide_tone(
        ctx(urgency=0.9, emotion="excited", owner_msg="haha ship it")
    )
    assert decision.roast == 1


def test_rule6_base_one_banter_bonus_excited_bonus() -> None:
    assert decide_tone(ctx(owner_msg="what's next")).roast == 1
    assert decide_tone(ctx(owner_msg="lol nice")).roast == 2
    assert decide_tone(ctx(emotion="excited", owner_msg="it worked")).roast == 2


def test_rule6_three_requires_explicit_request() -> None:
    assert decide_tone(ctx(emotion="excited", owner_msg="haha")).roast == 2
    assert decide_tone(ctx(owner_msg="roast me")).roast == 3


def test_company_caps_roast_and_kills_profanity() -> None:
    decision = decide_tone(ctx(audience="company", owner_msg="roast me"))
    assert decision.roast == 1
    assert decision.profanity is False


def test_on_behalf_carries_no_evie_voice() -> None:
    decision = decide_tone(ctx(audience="on_behalf", owner_msg="roast me"))
    assert decision.roast == 0
    assert decision.humour_allowed is False
    assert decision.technique is None
    assert decision.profanity is False
    assert decision.voice == "owner"


def test_voice_caps_roast_at_two() -> None:
    decision = decide_tone(ctx(channel="voice", owner_msg="roast me"))
    assert decision.roast == 2


def test_phone_caps_roast_at_two() -> None:
    decision = decide_tone(ctx(channel="phone", owner_msg="roast me"))
    assert decision.roast == 2


def test_slider_sets_ceiling() -> None:
    assert decide_tone(ctx(owner_msg="roast me", humor_slider=0)).roast == 0
    assert decide_tone(ctx(owner_msg="roast me", humor_slider=1)).roast == 1
    assert decide_tone(ctx(owner_msg="hi", humor_slider=5)).roast == 1


def test_unknown_signals_degrade_toward_calm() -> None:
    decision = decide_tone(
        ctx(emotion="ecstatic", urgency=None, owner_msg="???", audience="crowd")
    )
    assert decision.audience == "private"
    assert decision.roast == 1
    assert "ecstatic" not in " ".join(decision.reasons)


def test_joke_flag_helpers() -> None:
    allowed = decide_tone(ctx(owner_msg="hi"))
    assert joke_flag("some reply", allowed) is True
    assert joke_flag("", allowed) is False
    off = decide_tone(ctx(confirmation_pending=True))
    assert joke_flag("some reply", off) is False


def test_shape_hint_from_turn_shape() -> None:
    from app.persona.tone import decide_shape

    ack = ctx(owner_msg="ok", intent="general")
    shape, reason = decide_shape(ack)
    assert shape in ("one-liner", "short", "full")
    assert reason.startswith("shape: turn_shape=")
    assert decide_tone(ack).shape == shape


def test_shape_varies_with_seed_but_stays_deterministic() -> None:
    from app.persona.tone import decide_shape

    shapes = {
        decide_shape(ctx(owner_msg="ok thanks", thread_id="v", turn_index=i))[0]
        for i in range(40)
    }
    assert shapes <= {"one-liner", "short"}
    assert len(shapes) == 2
    first = decide_shape(ctx(owner_msg="hi", thread_id="t", turn_index=3))
    second = decide_shape(ctx(owner_msg="hi", thread_id="t", turn_index=3))
    assert first == second


def test_shape_rendered_on_every_decision() -> None:
    decision = decide_tone(ctx(owner_msg="hi"))
    assert decision.shape in ("one-liner", "short", "full")
    assert any(reason.startswith("shape:") for reason in decision.reasons)


def test_every_decision_carries_reasons() -> None:
    for kwargs in (
        {"confirmation_pending": True},
        {"emotion": "sad"},
        {"serious_latch_turns_left": 1},
        {"emotion": "frustrated"},
        {"urgency": 1.0},
        {"owner_msg": "hi"},
    ):
        decision = decide_tone(ctx(**kwargs))
        assert decision.reasons, kwargs
