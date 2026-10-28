"""Validator: fabricated bits, uncited items, pressure language, clamps."""

from __future__ import annotations

from datetime import UTC, datetime

from app.persona_mind.state import new_state
from app.persona_mind.validate import find_pressure, validate_output


def _known() -> dict:
    return {
        "t1": {
            "owner_msg": "haha that one got me, the hostage situation line",
            "evie_reply": "That's not working, that's a hostage situation.",
            "ts": "2026-01-01T00:00:00+00:00",
            "positive_after": True,
        },
        "t2": {
            "owner_msg": "not funny, focus",
            "evie_reply": "Some joke about deploys.",
            "ts": "2026-01-01T01:00:00+00:00",
            "positive_after": False,
        },
    }


def _state():
    state = new_state("o", datetime.now(UTC))
    state["opinions"] = [{
        "topic": "tabs", "stance": "tabs win", "reason": "alignment",
        "confidence": 0.7, "formed_at": "", "updated_at": "", "evidence": [],
    }]
    state["recommendations"] = [{
        "id": "rec-1", "text": "do x", "domain": "testing",
        "ts": "", "turn_id": "t1", "outcome": None,
    }]
    return state


def test_valid_output_passes_clean():
    raw = {
        "affect_deltas": {"playfulness": 0.1},
        "mood_words": [{"word": "intrigued", "dimension": "curiosity"}],
        "bit_candidates": [{"phrase": "hostage situation", "turn_ids": ["t1"]}],
        "narrative": "Late grind. He is close to done.",
    }
    result = validate_output(raw, known_turns=_known(), state=_state())
    assert result.rejected == []
    assert result.valid["affect_deltas"] == {"playfulness": 0.1}
    assert result.valid["bit_candidates"][0]["phrase"] == "hostage situation"


def test_fabricated_bit_rejected():
    raw = {"bit_candidates": [{"phrase": "never happened", "turn_ids": ["t1"]}]}
    result = validate_output(raw, known_turns=_known(), state=_state())
    assert result.valid["bit_candidates"] == []
    assert result.rejected[0][0] == "bit_candidates"
    assert "exact substring" in result.rejected[0][2]


def test_bit_without_positive_reaction_rejected():
    raw = {"bit_candidates": [{"phrase": "Some joke about deploys", "turn_ids": ["t2"]}]}
    result = validate_output(raw, known_turns=_known(), state=_state())
    assert result.valid["bit_candidates"] == []
    assert "positive reaction" in result.rejected[0][2]


def test_bit_citing_unknown_turn_rejected():
    raw = {"bit_candidates": [{"phrase": "hostage situation", "turn_ids": ["nope"]}]}
    result = validate_output(raw, known_turns=_known(), state=_state())
    assert result.valid["bit_candidates"] == []
    assert "unknown turn_id" in result.rejected[0][2]


def test_opinion_citing_unknown_turn_rejected():
    raw = {"new_opinions": [{
        "topic": "x", "stance": "y", "reason": "z",
        "confidence": 0.6, "turn_ids": ["nope"],
        "aliases": ["ex why", "zed topic"],
    }]}
    result = validate_output(raw, known_turns=_known(), state=_state())
    assert result.valid["opinion_updates"] == []
    assert result.rejected[0][2] == "cites unknown turn_id"


def test_new_opinion_requires_aliases():
    base = {"topic": "x", "stance": "y", "reason": "z",
            "confidence": 0.6, "turn_ids": ["t1"]}
    result = validate_output({"new_opinions": [base]},
                             known_turns=_known(), state=_state())
    assert result.valid["opinion_updates"] == []
    assert "2-5 aliases" in result.rejected[0][2]

    base["aliases"] = ["ex why", "zed topic"]
    result = validate_output({"new_opinions": [base]},
                             known_turns=_known(), state=_state())
    assert len(result.valid["opinion_updates"]) == 1


def test_alias_shape_enforced():
    for bad in (["OnlyOne"], ["shouty ALIAS", "ok two"], ["x" * 41, "ok two"]):
        raw = {"new_opinions": [{
            "topic": "x", "stance": "y", "reason": "z",
            "confidence": 0.6, "turn_ids": ["t1"], "aliases": bad,
        }]}
        result = validate_output(raw, known_turns=_known(), state=_state())
        assert result.valid["opinion_updates"] == [], bad
    # Updates may omit aliases entirely.
    raw = {"opinion_updates": [{
        "topic": "tabs", "stance": "tabs win", "reason": "alignment",
        "confidence": 0.75, "turn_ids": ["t1"],
    }]}
    result = validate_output(raw, known_turns=_known(), state=_state())
    assert len(result.valid["opinion_updates"]) == 1


def test_confidence_move_capped_unless_proven_wrong():
    base = {"topic": "tabs", "stance": "tabs win", "reason": "alignment",
            "confidence": 0.1, "turn_ids": ["t1"]}
    result = validate_output({"opinion_updates": [base]},
                             known_turns=_known(), state=_state())
    assert result.valid["opinion_updates"] == []
    assert "exceeds 0.3" in result.rejected[0][2]

    base["proven_wrong"] = True
    result = validate_output({"opinion_updates": [base]},
                             known_turns=_known(), state=_state())
    assert len(result.valid["opinion_updates"]) == 1


def test_outcome_unknown_recommendation_rejected():
    raw = {"outcomes": [{
        "recommendation_id": "rec-999", "result": "hit", "turn_ids": ["t1"],
    }]}
    result = validate_output(raw, known_turns=_known(), state=_state())
    assert result.valid["outcomes"] == []
    assert "unknown recommendation_id" in result.rejected[0][2]


def test_receipt_payload_keys_rejected():
    raw = {"recommendations": [{
        "text": "do x", "domain": "tests", "turn_id": "t1",
        "args": {"path": "/secret"},
    }]}
    result = validate_output(raw, known_turns=_known(), state=_state())
    assert result.valid["recommendations"] == []
    assert "off limits" in result.rejected[0][2]


def test_pressure_language_rejected_everywhere():
    state = _state()
    assert find_pressure("You never listen to me") == "you never"
    assert find_pressure("after all I've done for you") is not None
    assert find_pressure("I need you or I can't cope") is not None
    assert find_pressure("feeling sharp and pleased") is None

    raw = {"narrative": "He left early. You never stay when I need you."}
    result = validate_output(raw, known_turns=_known(), state=state)
    assert "narrative" not in result.valid
    assert "pressure" in result.rejected[0][2]

    raw = {"intention_updates": [{
        "action": "add", "text": "finish the refactor",
        "why": "after all I've done he owes me", "turn_ids": ["t1"],
    }]}
    result = validate_output(raw, known_turns=_known(), state=state)
    assert result.valid["intention_updates"] == []
    assert "pressure" in result.rejected[0][2]


def test_narrative_sentence_cap():
    raw = {"narrative": "One. Two. Three."}
    result = validate_output(raw, known_turns=_known(), state=_state())
    assert "narrative" not in result.valid
    assert "2 sentences" in result.rejected[0][2]


def test_deltas_clamped_unknown_dims_rejected():
    raw = {"affect_deltas": {"playfulness": 0.9, "telepathy": 0.1, "warmth": "hot"}}
    result = validate_output(raw, known_turns=_known(), state=_state())
    assert result.valid["affect_deltas"] == {"playfulness": 0.25}
    reasons = [r[2] for r in result.rejected]
    assert any("unknown dimension" in r for r in reasons)
    assert any("not a number" in r for r in reasons)


def test_mood_word_must_match_dominant_dimension():
    state = _state()
    state["affect"] = {d: 0.1 for d in
                       ("playfulness", "warmth", "concern", "energy",
                        "impatience", "satisfaction", "curiosity")}
    state["affect"]["impatience"] = 0.9
    raw = {"mood_words": [
        {"word": "irritated", "dimension": "impatience"},
        {"word": "ecstatic", "dimension": "playfulness"},
    ]}
    result = validate_output(raw, known_turns=_known(), state=state)
    assert [w["word"] for w in result.valid["mood_words"]] == ["irritated"]
    assert "not dominant" in result.rejected[0][2]


def test_partial_validity_keeps_good_items():
    raw = {
        "affect_deltas": {"energy": 0.2},
        "bit_candidates": [{"phrase": "invented", "turn_ids": ["t1"]}],
    }
    result = validate_output(raw, known_turns=_known(), state=_state())
    assert result.valid["affect_deltas"] == {"energy": 0.2}
    assert len(result.rejected) == 1


def test_garbage_input_never_raises():
    result = validate_output("nope", known_turns={}, state=_state())
    assert result.valid == {}
    assert result.rejected[0][2] == "top level is not an object"
    result = validate_output({"affect_deltas": [1, 2], "mood_words": {}},
                             known_turns={}, state=_state())
    assert result.valid["affect_deltas"] == {}
    assert result.valid["mood_words"] == []
