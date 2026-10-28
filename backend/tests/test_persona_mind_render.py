"""Render: natural register, relevance scoring, budgets, determinism."""

from __future__ import annotations

from datetime import UTC, datetime

from app.persona_mind.render import (
    RULES,
    exact_match,
    pick_opinions,
    render_block,
)
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
         "evidence": ["t1"], "aliases": ["testing", "property testing"],
         "updated_at": "2026-10-28T00:00:00+00:00"},
        {"topic": "meetings", "stance": "most meetings should be messages",
         "reason": "six people times one hour", "confidence": 0.7,
         "evidence": [], "aliases": ["meetings"],
         "updated_at": "2026-10-20T00:00:00+00:00"},
    ]
    state["intentions"] = [
        {"text": "get the auth refactor finished", "why": "he keeps stalling"},
    ]
    state["threads"] = [
        {"text": "promised benchmark numbers", "why": "he asked"},
    ]
    state["narrative"] = "A stubborn run of red tests, then a win."
    return state


def test_natural_register_no_debug_markers():
    state = _rich_state()
    text, _ = render_block(state, state["affect"], "testing auth", 200)
    assert "You're smug and sharp." in text
    assert "On testing: property tests catch real bugs — you're fairly sure." in text
    assert "You want to get the auth refactor finished." in text
    assert "Promised benchmark numbers is still open." in text
    assert "Lately: A stubborn run of red tests, then a win." in text
    assert "You want:" not in text and "Open:" not in text
    assert "(pleased" not in text and "you lean," not in text
    assert "0.8" not in text


def test_mood_without_words_uses_vector():
    state = _rich_state()
    state["mood_words"] = []
    text, _ = render_block(state, state["affect"], "", 120)
    assert "You're playful and a bit sharp." in text


def test_exact_topic_match_wins():
    state = _rich_state()
    state["opinions"].append({
        "topic": "sleep", "stance": "sleep is a performance tool",
        "reason": "rest first", "confidence": 0.6, "evidence": [],
        "aliases": ["sleep"], "updated_at": "2026-10-20T00:00:00+00:00",
    })
    picked = pick_opinions(state["opinions"], "should I sleep", 2)
    assert [op["topic"] for op in picked] == ["sleep"]  # meetings stays out


def test_stopwords_never_create_topicality():
    opinions = [{
        "topic": "meetings", "stance": "most meetings should be messages",
        "reason": "six people times one hour", "confidence": 0.7,
        "evidence": [], "aliases": ["meetings"],
        "updated_at": "2026-10-20T00:00:00+00:00",
    }]
    assert pick_opinions(opinions, "should I sleep", 1) == []
    assert pick_opinions(opinions, "what is open", 1) == []


def test_containment_matches_naming_variants():
    assert exact_match("microservices layout", "service", ["service"])
    assert not exact_match("heart surgery", "art", ["art"])
    state = _rich_state()
    state["opinions"].append({
        "topic": "auth service split", "stance": "keep the monolith",
        "reason": "one user", "confidence": 0.65, "evidence": [],
        "aliases": ["service split"], "updated_at": "2026-10-20T00:00:00+00:00",
    })
    picked = pick_opinions(state["opinions"], "microservices layout", 1)
    assert picked and picked[0]["topic"] == "auth service split"


def test_evidence_text_counts_for_relevance():
    state = _rich_state()
    state["opinions"].append({
        "topic": "auth service split", "stance": "keep it together",
        "reason": "one user", "confidence": 0.65, "evidence": ["t1"],
        "evidence_text": {"t1": "starting the auth refactor tonight split or monolith"},
        "aliases": ["zzlagoon"], "updated_at": "2026-10-20T00:00:00+00:00",
    })
    picked = pick_opinions(state["opinions"], "auth refactor status", 1)
    assert picked and picked[0]["topic"] == "auth service split"


def test_alias_match_counts_as_exact():
    opinions = [{
        "topic": "auth service split", "stance": "keep the monolith",
        "reason": "one user", "confidence": 0.6, "evidence": [],
        "aliases": ["auth service", "microservices", "monolith"],
        "updated_at": "2026-10-20T00:00:00+00:00",
    }]
    assert exact_match("microservices layout", "auth service split",
                       ["auth service", "microservices", "monolith"])
    picked = pick_opinions(opinions, "microservices layout", 1)
    assert picked and picked[0]["topic"] == "auth service split"


def test_no_opinion_below_threshold():
    state = _rich_state()
    picked = pick_opinions(state["opinions"], "quantum croissants", 2)
    assert picked == []
    text, _ = render_block(state, state["affect"], "quantum croissants", 200)
    assert "On testing:" not in text and "On meetings:" not in text


def test_full_budgets_fit():
    state = _rich_state()
    text, size = render_block(state, state["affect"], "testing auth", TEXT_BUDGET)
    assert size <= TEXT_BUDGET
    assert "MIND" in text and RULES in text
    assert "You're smug and sharp." in text

    voice_text, voice_size = render_block(
        state, state["affect"], "testing auth", VOICE_BUDGET
    )
    assert voice_size <= VOICE_BUDGET
    assert voice_text.startswith("MIND")


def test_truncation_order_narrative_first():
    state = _rich_state()
    texts = {}
    for budget in (200, 140, 120, 100, 80, 60):
        text, size = render_block(state, state["affect"], "testing", budget)
        assert size <= budget
        texts[budget] = text
    assert "Lately:" in texts[200]
    assert "Lately:" not in texts[100]
    assert "You want to" in texts[120]
    assert "You want to" not in texts[80]
    assert "On testing:" in texts[120]
    assert "On testing:" not in texts[80]
    assert "You're smug and sharp." in texts[60]


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
