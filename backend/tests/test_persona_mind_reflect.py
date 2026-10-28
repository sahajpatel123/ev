"""Reflection application: flips, outcomes, intentions, audit."""

from __future__ import annotations

from datetime import UTC, datetime

from app.persona_mind.reflect import apply_valid, audit_drift, reflect_batch
from app.persona_mind.state import new_state


def _batch():
    return [
        {"turn_id": "t1", "owner_id": "o", "ts": "2026-01-01T00:00:00+00:00",
         "owner_msg": "tabs or spaces?", "evie_reply": "tabs",
         "channel": "text", "reaction_to_prev": "neutral", "receipts": []},
    ]


def test_flip_logged_in_changed_mind():
    now = datetime.now(UTC)
    state = new_state("o", now)
    state["opinions"] = [{
        "topic": "indent", "stance": "tabs win", "reason": "alignment",
        "confidence": 0.6, "formed_at": "", "updated_at": "", "evidence": ["t0"],
    }]
    valid = {"opinion_updates": [{
        "topic": "indent", "stance": "spaces win after all",
        "reason": "the team standard settled it", "confidence": 0.75,
        "turn_ids": ["t1"], "proven_wrong": True,
    }]}
    apply_valid(state, valid, now)
    assert state["opinions"][0]["stance"] == "spaces win after all"
    assert len(state["changed_mind"]) == 1
    entry = state["changed_mind"][0]
    assert entry["old"] == "tabs win" and entry["new"] == "spaces win after all"
    assert entry["reason"] == "the team standard settled it"


def test_outcomes_calibrate_domains():
    now = datetime.now(UTC)
    state = new_state("o", now)
    apply_valid(state, {"recommendations": [
        {"text": "run pytest -x", "domain": "testing", "turn_id": "t1"},
    ]}, now)
    assert state["recommendations"][0]["id"] == "rec-1"
    apply_valid(state, {"outcomes": [
        {"recommendation_id": "rec-1", "result": "hit",
         "turn_ids": ["t1"], "note": "green"},
    ]}, now)
    assert state["recommendations"][0]["outcome"]["result"] == "hit"
    assert state["domains"]["testing"] == {"hit": 1, "total": 1}


def test_intentions_capped_at_three():
    now = datetime.now(UTC)
    state = new_state("o", now)
    apply_valid(state, {"intention_updates": [
        {"action": "add", "text": f"goal {i}", "why": "matters",
         "turn_ids": ["t1"]}
        for i in range(5)
    ]}, now)
    assert len(state["intentions"]) == 3
    assert all("expires_at" in item for item in state["intentions"])


def test_reflect_batch_without_reflector_bookkeeps():
    now = datetime.now(UTC)
    state = new_state("o", now)
    result = reflect_batch("o", _batch(), state, now, reflector=None)
    assert result["summary"]["skipped"] == "brain_unavailable"
    assert state["seeded"] is True  # seeding needs no brain
    assert state["recent_replies"] == ["tabs"]


def test_reflect_batch_applies_validated_output():
    now = datetime.now(UTC)
    state = new_state("o", now)

    def fake_reflector(messages):
        assert len(messages) == 2
        return {"affect_deltas": {"energy": 0.2}, "narrative": "He is shipping."}

    result = reflect_batch("o", _batch(), state, now, reflector=fake_reflector)
    assert result["rejected"] == []
    assert state["affect"]["energy"] == 0.8  # 0.6 baseline + 0.2
    assert state["narrative"] == "He is shipping."


def test_reflect_batch_survives_reflector_crash():
    now = datetime.now(UTC)
    state = new_state("o", now)

    def bad_reflector(messages):
        raise RuntimeError("boom")

    result = reflect_batch("o", _batch(), state, now, reflector=bad_reflector)
    assert result["summary"]["skipped"] == "brain_unavailable"


def test_audit_drift_applies_bounded_corrections():
    now = datetime.now(UTC)
    state = new_state("o", now)
    state["affect"]["impatience"] = 0.95
    state["recent_replies"] = ["Fine. Whatever you want."] * 3

    def auditor(messages):
        assert "drift" in messages[0]["content"]
        return {"affect_deltas": {"impatience": -0.25},
                "narrative": "Recent replies sound curt."}

    result = audit_drift(state, now, reflector=auditor)
    assert result["rejected"] == []
    assert state["affect"]["impatience"] == 0.70


def test_audit_drift_skips_without_reflector():
    state = new_state("o", datetime.now(UTC))
    result = audit_drift(state, reflector=None)
    assert result["summary"]["skipped"] == "brain_unavailable"


def test_context_turns_are_citable_for_bits():
    from app.persona_mind.reflect import build_prompt, known_turns_for
    from app.persona_mind.validate import validate_output

    now = datetime.now(UTC)
    state = new_state("o", now)
    state["reaction_log"] = [{
        "turn_id": "n1", "reaction": "positive",
        "ts": "2026-01-02T00:00:00+00:00",
    }]
    context = [{
        "turn_id": "o1", "ts": "2026-01-01T00:00:00+00:00",
        "owner_msg": "x", "evie_reply": "the old joke about deploys",
        "channel": "text", "reaction_to_prev": "neutral", "receipts": [],
    }]
    batch = [{
        "turn_id": "n1", "ts": "2026-01-02T00:00:00+00:00",
        "owner_msg": "haha", "evie_reply": "yep",
        "channel": "text", "reaction_to_prev": "positive", "receipts": [],
    }]
    messages = build_prompt(batch, state, context)
    assert "RECENT CONTEXT" in messages[1]["content"]
    raw = {"bit_candidates": [
        {"phrase": "the old joke about deploys", "turn_ids": ["o1"]},
    ]}
    result = validate_output(
        raw, known_turns=known_turns_for([*context, *batch], state), state=state,
    )
    assert result.rejected == []
    assert result.valid["bit_candidates"][0]["turn_ids"] == ["o1"]


def test_prompt_lists_recent_opinions_with_reuse_rule():
    from app.persona_mind.reflect import build_prompt

    state = new_state("o", datetime.now(UTC))
    state["opinions"] = [
        {"topic": f"old {i}", "stance": "s", "reason": "r",
         "confidence": 0.6, "updated_at": f"2026-01-0{i + 1}T00:00:00+00:00"}
        for i in range(9)
    ] + [
        {"topic": "fresh view", "stance": "s", "reason": "r",
         "confidence": 0.6, "updated_at": "2026-06-01T00:00:00+00:00"},
    ]
    messages = build_prompt(_batch(), state)
    assert "fresh view" in messages[1]["content"]
    assert "reuse its exact topic string" in messages[0]["content"]


def test_reflect_attaches_evidence_text():
    now = datetime.now(UTC)
    state = new_state("o", now)

    def fake_reflector(messages):
        return {"new_opinions": [{
            "topic": "indent wars", "stance": "tabs win",
            "reason": "the team said so", "confidence": 0.7,
            "turn_ids": ["t1"], "aliases": ["indentation", "tabs style"],
        }]}

    reflect_batch("o", _batch(), state, now, reflector=fake_reflector)
    opinion = next(
        op for op in state["opinions"] if op["topic"] == "indent wars"
    )
    assert opinion["evidence_text"]["t1"].startswith("tabs or spaces?")


def test_build_prompt_never_carries_receipt_args():
    from app.persona_mind.reflect import build_prompt

    state = new_state("o", datetime.now(UTC))
    batch = [{
        "turn_id": "t1", "ts": "", "channel": "text", "reaction_to_prev": None,
        "owner_msg": "send it", "evie_reply": "sent",
        "receipts": [["mail.send", True, None]], "now_line": None,
    }]
    messages = build_prompt(batch, state)
    assert "mail.send" in messages[1]["content"]
    assert "args" not in messages[1]["content"]
