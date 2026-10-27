"""Opinion ledger (spec §7): stances with timestamps and reasons.

Contradiction detection is deliberately narrow and deterministic: same
normalized topic, different normalized text. Anything subtler is left
for the model and the owner, never guessed.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from app.persona.callbacks import UnwiredStoreError


@dataclass(frozen=True)
class Stance:
    topic: str
    text: str
    reason: str
    ts: str


def _normalize_topic(topic: str) -> str:
    return re.sub(r"\s+", " ", (topic or "").strip().lower())


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


class OpinionLedger(Protocol):
    """What persona needs from stance persistence."""

    def record(self, stance: Stance) -> None: ...
    def recent(self, topic: str, limit: int = 5) -> list[Stance]: ...
    def contradicts(self, new_stance: Stance) -> list[Stance]: ...


class InMemoryOpinionLedger:
    """Process-local ledger. Deterministic; nothing leaves the process."""

    def __init__(self) -> None:
        self._stances: list[Stance] = []

    def record(self, stance: Stance) -> None:
        self._stances.append(stance)

    def recent(self, topic: str, limit: int = 5) -> list[Stance]:
        wanted = _normalize_topic(topic)
        matches = [
            stance
            for stance in self._stances
            if _normalize_topic(stance.topic) == wanted
        ]
        return matches[-max(1, limit) :]

    def contradicts(self, new_stance: Stance) -> list[Stance]:
        wanted_topic = _normalize_topic(new_stance.topic)
        wanted_text = _normalize_text(new_stance.text)
        return [
            stance
            for stance in self._stances
            if _normalize_topic(stance.topic) == wanted_topic
            and _normalize_text(stance.text) != wanted_text
        ]


class EventMetadataOpinionLedger:
    """Adapter over namespaced Event.metadata (unwired until an owner injects
    ``read``/``write`` callables; no app imports, so persona stays pure)."""

    NAMESPACE = "persona_opinions"

    def __init__(
        self,
        read: Callable[[str], dict[str, Any] | None] | None = None,
        write: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._read = read
        self._write = write

    def _load(self) -> list[dict[str, Any]]:
        if self._read is None:
            raise UnwiredStoreError("EventMetadataOpinionLedger has no read callable")
        state = self._read(self.NAMESPACE) or {}
        return list(state.get("stances", []))

    def _save(self, stances: list[dict[str, Any]]) -> None:
        if self._write is None:
            raise UnwiredStoreError("EventMetadataOpinionLedger has no write callable")
        self._write(self.NAMESPACE, {"stances": stances[-500:]})

    @staticmethod
    def _to_stance(raw: dict[str, Any]) -> Stance:
        return Stance(
            topic=str(raw.get("topic", "")),
            text=str(raw.get("text", "")),
            reason=str(raw.get("reason", "")),
            ts=str(raw.get("ts", "")),
        )

    def record(self, stance: Stance) -> None:
        stances = self._load()
        stances.append(
            {"topic": stance.topic, "text": stance.text,
             "reason": stance.reason, "ts": stance.ts}
        )
        self._save(stances)

    def recent(self, topic: str, limit: int = 5) -> list[Stance]:
        wanted = _normalize_topic(topic)
        matches = [
            self._to_stance(raw)
            for raw in self._load()
            if _normalize_topic(str(raw.get("topic", ""))) == wanted
        ]
        return matches[-max(1, limit) :]

    def contradicts(self, new_stance: Stance) -> list[Stance]:
        wanted_topic = _normalize_topic(new_stance.topic)
        wanted_text = _normalize_text(new_stance.text)
        return [
            self._to_stance(raw)
            for raw in self._load()
            if _normalize_topic(str(raw.get("topic", ""))) == wanted_topic
            and _normalize_text(str(raw.get("text", ""))) != wanted_text
        ]
