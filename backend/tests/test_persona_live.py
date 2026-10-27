"""Live-turn + SQLite store tests (no foreign hooks, tmp paths only)."""

from __future__ import annotations

from app.persona.callbacks import InMemoryCallbackStore
from app.persona.context import JokeMeta, PersonaContext
from app.persona.learning import LearningWeights
from app.persona.live import (
    after_decision,
    begin_turn,
    extract_stances,
    key_for,
    note_turn,
)
from app.persona.opinions import InMemoryOpinionLedger
from app.persona.store import PersonaStore
from app.persona.tone import decide_tone


def ctx(**kwargs) -> PersonaContext:
    kwargs.setdefault("channel", "text")
    return PersonaContext(**kwargs)


def test_key_for_thread_or_owner_fallback() -> None:
    assert key_for(thread_id="abc") == "thread:abc"
    assert key_for() == "owner:default"
    assert key_for(session_id="s1") == "owner:s1"


def test_begin_turn_fresh_and_roundtrip(tmp_path) -> None:
    store = PersonaStore(tmp_path / "p.db", key="thread:t1")
    first = begin_turn(store, "thread:t1")
    assert first.latch == 0
    assert first.session_started is not None
    second = begin_turn(store, "thread:t1")
    assert second.session_started == first.session_started
    store.close()


def test_note_turn_learns_and_persists(tmp_path) -> None:
    store = PersonaStore(tmp_path / "p.db", key="thread:t1")
    state = begin_turn(store, "thread:t1")
    meta = JokeMeta(technique="understatement", topic="code")
    new_state, reaction = note_turn(
        store, None, "thread:t1", state, owner_msg="haha good one",
        prev_reply="Which Monday? You've announced a few.",
        prev_meta=meta,
    )
    assert reaction == "positive"
    assert new_state.weights.techniques["understatement"].alpha == 2.0
    reloaded = begin_turn(store, "thread:t1")
    assert reloaded.weights.techniques["understatement"].alpha == 2.0
    assert store.suggest_callback("thread:t1") is None  # only 1 positive
    store.close()


def test_landed_bit_callback_once_per_conversation(tmp_path) -> None:
    store = PersonaStore(tmp_path / "p.db", key="thread:t1")
    store.record_landed_bit("The font has had a longer interview.")
    assert store.suggest_callback("thread:t1") is None
    store.record_landed_bit("The font has had a longer interview.")
    assert store.suggest_callback("thread:t1") == "The font has had a longer interview."
    store.confirm_callback("thread:t1", "The font has had a longer interview.")
    assert store.suggest_callback("thread:t1") is None
    assert store.suggest_callback("thread:t2") == "The font has had a longer interview."
    store.close()


def test_no_repeat_lines_persist(tmp_path) -> None:
    store = PersonaStore(tmp_path / "p.db")
    store.record_line("Never repeat this line.")
    assert store.has_line("never repeat this line.") is True
    reopened = PersonaStore(tmp_path / "p.db")
    assert reopened.has_line("NEVER repeat this line.") is True
    store.close()
    reopened.close()


def test_ledger_roundtrip_and_contradiction(tmp_path) -> None:
    from app.persona.opinions import Stance

    store = PersonaStore(tmp_path / "p.db")
    store.record(Stance("orm", "Use the ORM", "speed", "t1"))
    assert [s.text for s in store.recent("ORM")] == ["Use the ORM"]
    assert store.contradicts(Stance("orm", "Use raw SQL", "x", "t2"))
    assert not store.contradicts(Stance("orm", "Use the ORM", "x", "t2"))
    store.close()


def test_extract_stances_and_ledger_line() -> None:
    from app.persona.compile import build_persona_block

    stances = extract_stances("I'd pick the monolith because it ships.", "code")
    assert len(stances) == 1
    assert stances[0].reason == "it ships"
    assert extract_stances("hello there friend", "code") == []
    ledger = InMemoryOpinionLedger()
    from app.persona.opinions import Stance

    ledger.record(Stance("code", "Use the ORM", "speed", "t1"))
    block = build_persona_block(
        ctx(owner_msg="I think we should use raw SQL for this API now"),
        ledger=ledger,
    )
    assert "LEDGER:" in block.text
    assert block.token_estimate <= 750


def test_after_decision_persists_latch_flags_meta(tmp_path) -> None:
    store = PersonaStore(tmp_path / "p.db", key="thread:t1")
    state = begin_turn(store, "thread:t1")
    decision = decide_tone(ctx(owner_msg="hi"), LearningWeights.default())
    new_state = after_decision(store, "thread:t1", state, decision,
                               "A fine reply here today.", owner_msg="hi")
    assert new_state.recent_joke_flags == (True,)
    assert (new_state.last_meta is None) == (decision.technique is None)
    if decision.technique is not None:
        assert new_state.last_meta.technique == decision.technique
    assert store.has_line("a fine reply here today.") is True
    store.close()


def test_memory_store_callback_bits() -> None:
    store = InMemoryCallbackStore()
    store.record_landed_bit("bit one here")
    assert store.suggest_callback("k") is None
    store.record_landed_bit("bit one here")
    assert store.suggest_callback("k") == "bit one here"
    store.confirm_callback("k", "bit one here")
    assert store.suggest_callback("k") is None


def test_store_keys_are_isolated(tmp_path) -> None:
    left = PersonaStore(tmp_path / "p.db", key="thread:left")
    right = PersonaStore(tmp_path / "p.db", key="thread:right")
    left.save_weights(LearningWeights.default())
    right.record_outcome("escalation", "code", "negative")
    assert left.load_weights().techniques["escalation"].beta == 1.0
    assert right.load_weights().techniques["escalation"].beta == 2.0
    left.close()
    right.close()
