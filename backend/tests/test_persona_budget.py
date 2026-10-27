"""Budget tests (v2.1): text ≤ 750, voice ≤ 450, CORE never dropped.

Drop order: exemplars, then TEXT ADDENDUM, then NOW-line detail
(full -> short -> clock-only -> omitted).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.persona.compile import TEXT_BUDGET, VOICE_BUDGET, build_persona_block
from app.persona.context import PersonaContext, WorkState
from app.persona.core import addendum_only, core_only, operational_lines
from app.persona.exemplars import MAX_EXEMPLARS


def ctx(**kwargs) -> PersonaContext:
    kwargs.setdefault("channel", "text")
    return PersonaContext(**kwargs)


def test_budgets_are_750_and_450() -> None:
    assert TEXT_BUDGET == 750
    assert VOICE_BUDGET == 450


def test_core_and_addendum_sizes() -> None:
    from app.persona.tokens import estimate

    assert estimate(core_only()) <= 395
    assert estimate(addendum_only()) <= 90
    assert "persona.v2.1" in core_only()
    assert "persona.v2.1" not in addendum_only()


def test_text_block_fits_budget_typical_turn() -> None:
    block = build_persona_block(ctx(owner_msg="Microservices for my MVP?"))
    assert block.token_estimate <= TEXT_BUDGET
    assert block.version == "persona.v2.1"


def test_text_block_fits_budget_worst_case_signals() -> None:
    block = build_persona_block(ctx(
        owner_msg="roast me, I can take it",
        emotion="excited",
        urgency=0.9,
        intent="decision",
        work_state=WorkState(task="t" * 300, last_result="r" * 300),
        first_commitment_tomorrow="c" * 100,
    ))
    assert block.token_estimate <= TEXT_BUDGET
    assert "Errors, confirmations, deletions, money:" in block.text
    assert "TURN TONE:" in block.text


def test_voice_block_fits_450_on_every_path() -> None:
    cases = [
        ctx(channel="voice", owner_msg="hi"),
        ctx(channel="voice", owner_msg="roast me"),
        ctx(channel="phone", owner_msg="roast me"),
        ctx(channel="voice", emotion="frustrated", owner_msg="ugh, again"),
        ctx(channel="voice", audience="on_behalf", owner_msg="draft it"),
        ctx(channel="voice", owner_msg="x" * 500,
            work_state=WorkState(task="t" * 300)),
        ctx(channel="voice", local_time=datetime(2026, 10, 27, 14, 5),
            session_started=datetime(2026, 10, 27, 9, 0),
            owner_msg="status update please"),
    ]
    for case in cases:
        block = build_persona_block(case)
        assert block.token_estimate <= VOICE_BUDGET, (case, block.token_estimate)
        assert "Errors, confirmations, deletions, money:" in block.text


def test_drop_order_exemplars_then_addendum_then_now() -> None:
    rich = ctx(
        owner_msg="my code works but why",
        local_time=datetime(2026, 10, 27, 14, 5),
        session_started=datetime(2026, 10, 27, 9, 0),
        work_state=WorkState(task="auth refactor", last_result="3 failing"),
        first_commitment_tomorrow="10:00 class",
    )
    block = build_persona_block(rich)
    assert block.token_estimate <= TEXT_BUDGET
    # Text has room: addendum stays, exemplars may stay.
    assert addendum_only().rstrip() in block.text
    # Force pressure with a tiny budget: exemplars shed before addendum.
    tight = build_persona_block(rich, budget=460)
    assert tight.token_estimate <= 460
    kinds = [entry.split(":")[0] for entry in tight.dropped]
    assert kinds[0] == "exemplar"
    assert "addendum" in kinds  # addendum shed only after exemplars


def test_core_lines_never_dropped() -> None:
    block = build_persona_block(
        ctx(owner_msg="x" * 1000), budget=200)
    for line in core_only().splitlines():
        if line.strip():
            assert line in block.text, line


def test_operational_lines_present_on_every_path() -> None:
    cases = [
        ctx(channel="text", owner_msg="hi"),
        ctx(channel="voice", owner_msg="hi"),
        ctx(channel="phone", owner_msg="roast me"),
        ctx(channel="text", confirmation_pending=True),
        ctx(channel="text", audience="on_behalf"),
        ctx(channel="text", audience="company", owner_msg="roast me"),
    ]
    for case in cases:
        block = build_persona_block(case)
        assert operational_lines(case.channel) in block.text, case
        assert "Say \"done\" only when the result confirms it." in block.text


def test_voice_has_no_addendum_text_keeps_it() -> None:
    voice = build_persona_block(ctx(channel="voice", owner_msg="hi"))
    assert "If he mixes Hindi and English" not in voice.text
    text = build_persona_block(ctx(channel="text", owner_msg="hi"))
    assert "If he mixes Hindi and English" in text.text


def test_tone_line_format_with_shape() -> None:
    block = build_persona_block(ctx(owner_msg="hi"))
    line = next(part for part in block.text.splitlines() if part.startswith("TURN TONE:"))
    assert line.startswith("TURN TONE: ROAST=1; AUDIENCE=private; PROFANITY=off; TECHNIQUE=")
    assert "; SHAPE=" in line
    assert block.tone.shape in ("one-liner", "short", "full")


def test_max_two_exemplars() -> None:
    assert MAX_EXEMPLARS == 2
    block = build_persona_block(ctx(owner_msg="my code works but why"))
    assert block.text.count("[exemplar") <= MAX_EXEMPLARS


@pytest.mark.parametrize("channel", ["text", "voice", "phone"])
def test_determinism_same_context_same_block(channel: str) -> None:
    case = ctx(channel=channel, owner_msg="roast me", thread_id="t", turn_index=7)
    first = build_persona_block(case)
    second = build_persona_block(case)
    assert first.text == second.text
    assert first.tone == second.tone
    assert first.token_estimate == second.token_estimate
