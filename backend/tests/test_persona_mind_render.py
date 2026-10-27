"""Render: budgets, truncation order, determinism, rules."""

from __future__ import annotations

from datetime import UTC, datetime

from app.persona_mind.render import RULES, estimate, render_block
from app.persona_mind.settings import TEXT_BUDGET, VOICE_BUDGET
from app.persona_mind.state import new_state


def _rich_state():
    state = new_state("o", datetime.now(UTC))
    state["affect"] = {
        "playfulness": 0.8, "warmth": 0.7, "concern": 0.5, "energy": 0.75,
        "impatience": 0.2, "satisfaction": 0.7, "curiosity": 0.6,
    }
    state["mood_words"] = [
        {"word": "smug", "dimension": "satisfaction"},
        {"word": "sharp", "dimension": "energy"},
    ]
    state["opinions"] = [
        {"topic": "testing", "stance": "property tests catch real bugs",
         "reason": "randomized inputs explore corners", "confidence": 0.8,
         "evidence": ["t1"]},
        {"topic": "meetings", "stance": "most meetings should be messages",
         "reason": "six people times one hour", "confidence": 0.7, "evidence": []},
    ]
    state["intentions"] = [
        {"text": "get the auth refactor finished", "why": "he keeps stalling"},
        {"text": "nudge him to sleep before 3am", "why": "late again"},
    ]
    state["threads"] = [
        {"text": "promised benchmark numbers", "why": "he asked"},
        {"text": "curious about the new router", "why": "mentioned once"},
    ]
    state["narrative"] = "Late grind on auth. He is close to green."
    return state


def test_estimate_matches_repo_convention():
    assert estimate("") == 1
    assert estimate("abcd") == 1
    assert estimate("a" * 400) == 100


def test_full_budgets_fit():
    state = _rich_state()
    text, size = render_block(state, state["affect"], "testing auth", TEXT_BUDGET)
    assert size <= TEXT_BUDGET
    assert "MIND" in text and RULES in text
    assert "smug" in text and "auth refactor" in text

    full, _ = render_block(state, state["affect"], "testing auth", 200)
    assert "Late grind" in full  # narrative survives only with room

    voice_text, voice_size = render_block(
        state, state["affect"], "testing auth", VOICE_BUDGET
    )
    assert voice_size <= VOICE_BUDGET
    assert voice_text.startswith("MIND")


def test_truncation_order_narrative_first():
    state = _rich_state()
    texts = {}
    for budget in (200, 120, 100, 80, 60, 40):
        text, size = render_block(state, state["affect"], "testing", budget)
        assert size <= budget
        texts[budget] = text
    # Narrative goes before threads, threads before intentions.
    assert "Late grind" in texts[200]
    assert "Late grind" not in texts[120]
    assert "auth refactor" in texts[120]
    assert "auth refactor" not in texts[80]
    assert "On testing:" in texts[100]
    assert "On testing:" not in texts[80]
    assert "smug" in texts[60] or "feeling" in texts[60]


def test_tiny_budget_still_fits():
    state = _rich_state()
    text, size = render_block(state, state["affect"], "x", 20)
    assert size <= 20


def test_deterministic():
    state = _rich_state()
    first = render_block(state, state["affect"], "testing auth", 120)
    second = render_block(state, state["affect"], "testing auth", 120)
    assert first == second


def test_relevance_prefers_matching_opinion():
    state = _rich_state()
    text, _ = render_block(state, state["affect"], "property tests", 200)
    assert "On testing:" in text


def test_natural_language_no_numbers_dump():
    state = _rich_state()
    text, _ = render_block(state, state["affect"], "testing", 120)
    assert "0.8" not in text and "confidence 0" not in text
    assert "fairly sure" in text  # confidence wording, not a number
