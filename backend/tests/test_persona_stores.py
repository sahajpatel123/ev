"""Store tests: CallbackStore + OpinionLedger (InMemory + adapters)."""

from __future__ import annotations

import pytest

from app.persona.callbacks import (
    EventMetadataCallbackStore,
    InMemoryCallbackStore,
    UnwiredStoreError,
    normalize_line,
)
from app.persona.learning import Beta
from app.persona.opinions import (
    EventMetadataOpinionLedger,
    InMemoryOpinionLedger,
    Stance,
)


def test_normalize_line() -> None:
    assert normalize_line("  Hello   WORLD  ") == "hello world"
    assert normalize_line("") == ""


def test_memory_store_no_repeat_lines() -> None:
    store = InMemoryCallbackStore()
    assert store.has_line("Which Monday?") is False
    store.record_line("Which   Monday? ")
    assert store.has_line("which monday?") is True
    store.record_line("")
    assert store.has_line("") is False


def test_memory_store_landed_needs_two_positives() -> None:
    store = InMemoryCallbackStore()
    assert store.landed("callback", "code") is False
    store.record_outcome("callback", "code", "positive")
    assert store.landed("callback", "code") is False
    store.record_outcome("callback", "code", "positive")
    assert store.landed("callback", "code") is True
    store.record_outcome("callback", "code", "negative")
    assert store.landed("callback", "code") is True


def test_memory_store_weights_roundtrip() -> None:
    store = InMemoryCallbackStore()
    store.record_outcome("understatement", "code", "positive")
    weights = store.load_weights()
    assert weights.techniques["understatement"] == Beta(2.0, 1.0)
    assert weights.joke_rate == Beta(2.0, 1.0)


def test_event_metadata_store_unwired_raises() -> None:
    store = EventMetadataCallbackStore()
    with pytest.raises(UnwiredStoreError):
        store.has_line("x")
    with pytest.raises(UnwiredStoreError):
        store.record_line("x")
    ledger = EventMetadataOpinionLedger()
    with pytest.raises(UnwiredStoreError):
        ledger.recent("code")


def test_event_metadata_store_works_with_callables() -> None:
    backing: dict[str, dict] = {}
    store = EventMetadataCallbackStore(
        read=lambda namespace: backing.get(namespace),
        write=lambda namespace, state: backing.__setitem__(namespace, state),
    )
    store.record_line("Never repeat me")
    assert store.has_line("never repeat me") is True
    store.record_outcome("callback", "code", "positive")
    store.record_outcome("callback", "code", "positive")
    assert store.landed("callback", "code") is True
    assert store.load_weights().techniques["callback"] == Beta(3.0, 1.0)


def test_memory_ledger_record_recent_contradicts() -> None:
    ledger = InMemoryOpinionLedger()
    first = Stance("orm", "Use the ORM", "speed", "2026-10-20")
    ledger.record(first)
    ledger.record(Stance("orm", "Use raw SQL", "control", "2026-10-27"))
    recent = ledger.recent("ORM")
    assert [stance.text for stance in recent] == ["Use the ORM", "Use raw SQL"]
    clashes = ledger.contradicts(Stance("orm", "Use raw SQL", "control", "now"))
    assert [stance.text for stance in clashes] == ["Use the ORM"]
    assert ledger.contradicts(Stance("other", "Use raw SQL", "x", "now")) == []


def test_event_metadata_ledger_works_with_callables() -> None:
    backing: dict[str, dict] = {}
    ledger = EventMetadataOpinionLedger(
        read=lambda namespace: backing.get(namespace),
        write=lambda namespace, state: backing.__setitem__(namespace, state),
    )
    ledger.record(Stance("sleep", "Ship it tonight", "deadline", "t1"))
    assert [s.text for s in ledger.recent("sleep")] == ["Ship it tonight"]
    assert ledger.contradicts(Stance("sleep", "Sleep first", "rest", "t2"))
