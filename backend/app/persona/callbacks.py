"""Callback store: landed bits + the no-repeat line store (spec §6).

Persistence for learning weights also flows through this interface
(spec §4/§6: pure functions; the store owns state). Two implementations
ship: InMemory (tests, dry runs) and an EventMetadata adapter that talks
to the event store through injected callables, so this package imports
nothing outside itself (except the token estimator) and stays unwired
until an owner passes real callables.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Protocol

from app.persona import learning
from app.persona.learning import LearningWeights


def normalize_line(line: str) -> str:
    """Canonical form for no-repeat comparison."""
    return re.sub(r"\s+", " ", (line or "").strip().lower())


class CallbackStore(Protocol):
    """What compile/guard/learning need from persistence."""

    def has_line(self, line: str) -> bool: ...
    def record_line(self, line: str) -> None: ...
    def record_outcome(self, technique: str, topic: str, reaction: str) -> None: ...
    def load_weights(self) -> LearningWeights: ...
    def save_weights(self, weights: LearningWeights) -> None: ...
    def landed(self, technique: str, topic: str) -> bool: ...


class InMemoryCallbackStore:
    """Process-local store. Deterministic; nothing leaves the process."""

    def __init__(self) -> None:
        self._lines: set[str] = set()
        self._outcomes: list[tuple[str, str, str]] = []
        self._weights = LearningWeights.default()
        self._bits: dict[str, int] = {}
        self._used: set[tuple[str, str]] = set()

    def has_line(self, line: str) -> bool:
        normalized = normalize_line(line)
        return bool(normalized) and normalized in self._lines

    def record_line(self, line: str) -> None:
        normalized = normalize_line(line)
        if normalized:
            self._lines.add(normalized)

    def record_outcome(self, technique: str, topic: str, reaction: str) -> None:
        self._outcomes.append((technique, topic, reaction))
        self._weights = learning.update(self._weights, technique, topic, reaction)

    def load_weights(self) -> LearningWeights:
        return self._weights

    def save_weights(self, weights: LearningWeights) -> None:
        self._weights = weights

    def landed(self, technique: str, topic: str) -> bool:
        positives = sum(
            1
            for tech, top, reaction in self._outcomes
            if tech == technique and top == topic and reaction == "positive"
        )
        return positives >= 2

    def record_landed_bit(self, bit: str) -> None:
        text = " ".join((bit or "").split())
        if text:
            self._bits[text[:300]] = self._bits.get(text[:300], 0) + 1

    def suggest_callback(self, key: str | None = None) -> str | None:
        who = key or "owner:default"
        for bit in sorted(self._bits, key=lambda b: (-self._bits[b], b)):
            if self._bits[bit] >= 2 and (who, bit) not in self._used:
                return bit
        return None

    def confirm_callback(self, key: str, bit: str) -> None:
        self._used.add((key, bit))


class UnwiredStoreError(RuntimeError):
    """The event-metadata adapter was used without owner callables."""


class EventMetadataCallbackStore:
    """Adapter over namespaced Event.metadata (unwired until an owner injects
    ``read``/``write`` callables; no app imports, so persona stays pure)."""

    NAMESPACE = "persona_callback"

    def __init__(
        self,
        read: Callable[[str], dict[str, Any] | None] | None = None,
        write: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._read = read
        self._write = write

    def _state(self) -> dict[str, Any]:
        if self._read is None:
            raise UnwiredStoreError("EventMetadataCallbackStore has no read callable")
        return dict(self._read(self.NAMESPACE) or {})

    def _save(self, state: dict[str, Any]) -> None:
        if self._write is None:
            raise UnwiredStoreError("EventMetadataCallbackStore has no write callable")
        self._write(self.NAMESPACE, state)

    def has_line(self, line: str) -> bool:
        normalized = normalize_line(line)
        lines = self._state().get("lines", [])
        return bool(normalized) and normalized in set(lines)

    def record_line(self, line: str) -> None:
        normalized = normalize_line(line)
        if not normalized:
            return
        state = self._state()
        lines = list(state.get("lines", []))
        if normalized not in lines:
            lines.append(normalized)
        state["lines"] = lines[-500:]
        self._save(state)

    def record_outcome(self, technique: str, topic: str, reaction: str) -> None:
        state = self._state()
        outcomes = list(state.get("outcomes", []))
        outcomes.append({"technique": technique, "topic": topic, "reaction": reaction})
        state["outcomes"] = outcomes[-500:]
        weights = learning.update(self._weights_from(state), technique, topic, reaction)
        state["weights"] = learning.describe(weights)
        self._save(state)

    def load_weights(self) -> LearningWeights:
        return self._weights_from(self._state())

    def save_weights(self, weights: LearningWeights) -> None:
        state = self._state()
        state["weights"] = learning.describe(weights)
        self._save(state)

    def landed(self, technique: str, topic: str) -> bool:
        positives = sum(
            1
            for outcome in self._state().get("outcomes", [])
            if outcome.get("technique") == technique
            and outcome.get("topic") == topic
            and outcome.get("reaction") == "positive"
        )
        return positives >= 2

    def record_landed_bit(self, bit: str) -> None:
        text = " ".join((bit or "").split())
        if not text:
            return
        state = self._state()
        bits = dict(state.get("bits", {}))
        bits[text[:300]] = bits.get(text[:300], 0) + 1
        state["bits"] = bits
        self._save(state)

    def suggest_callback(self, key: str | None = None) -> str | None:
        who = key or "owner:default"
        state = self._state()
        bits = state.get("bits", {}) or {}
        used = {tuple(pair) for pair in state.get("used", [])}
        for bit in sorted(bits, key=lambda b: (-bits[b], b)):
            if bits[bit] >= 2 and (who, bit) not in used:
                return bit
        return None

    def confirm_callback(self, key: str, bit: str) -> None:
        state = self._state()
        used = list(state.get("used", []))
        if [key, bit] not in used:
            used.append([key, bit])
        state["used"] = used
        self._save(state)

    @staticmethod
    def _weights_from(state: dict[str, Any]) -> LearningWeights:
        raw = state.get("weights") or {}
        techniques_raw = raw.get("techniques", {}) or {}
        topics_raw = raw.get("topics", {}) or {}
        rate_raw = raw.get("joke_rate", {}) or {}
        return LearningWeights(
            techniques={
                key: learning.Beta(
                    float(value.get("alpha", 1.0)), float(value.get("beta", 1.0))
                )
                for key, value in techniques_raw.items()
                if isinstance(value, dict)
            },
            topics={
                key: learning.Beta(
                    float(value.get("alpha", 1.0)), float(value.get("beta", 1.0))
                )
                for key, value in topics_raw.items()
                if isinstance(value, dict)
            },
            joke_rate=learning.Beta(
                float(rate_raw.get("alpha", 1.0)), float(rate_raw.get("beta", 1.0))
            ),
        )
