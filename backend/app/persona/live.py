"""Live turn orchestration (§4.3): read state, learn, persist.

No foreign hooks: everything here runs inside persona-owned files (the
delegation wrappers in dynamic_personality, the lab) against the
persona-owned SQLite store. Keyed by thread id when a caller passes one,
else "owner:default" (documented degradation: one shared lane).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.persona import learning
from app.persona.context import JokeMeta, ToneDecision
from app.persona.learning import LearningWeights
from app.persona.nightshift import NightState
from app.persona.opinions import Stance
from app.persona.reactions import classify_reaction, topic_of
from app.persona.tone import joke_flag, update_latch

DEFAULT_KEY = "owner:default"

# Reply sentences that read as Evie taking a stance (ledger recording).
_STANCE_MARKERS = (
    re.compile(r"\bI'?d (pick|go with|say)\b", re.IGNORECASE),
    re.compile(r"\bI (prefer|think|believe|recommend)\b", re.IGNORECASE),
    re.compile(r"\bmy take\b", re.IGNORECASE),
    re.compile(r"\byou should\b", re.IGNORECASE),
    re.compile(r"\bship (the|a)\b", re.IGNORECASE),
    re.compile(r"^Noted\.", re.IGNORECASE),
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")


def key_for(thread_id: str | None = None, session_id: str | None = None) -> str:
    """Store key: thread id when available, else owner+session fallback."""
    if thread_id:
        return f"thread:{thread_id}"
    return f"owner:{session_id or 'default'}"


@dataclass(frozen=True)
class LiveState:
    """Per-key live persona state (persisted as one blob)."""

    latch: int = 0
    recent_joke_flags: tuple[bool, ...] = ()
    last_meta: JokeMeta | None = None
    weights: LearningWeights = None  # type: ignore[assignment]
    night: NightState | None = None
    session_started: str | None = None

    def __post_init__(self) -> None:
        if self.weights is None:
            object.__setattr__(self, "weights", LearningWeights.default())

    def to_blob(self) -> dict[str, Any]:
        night = self.night
        return {
            "latch": self.latch,
            "recent_joke_flags": list(self.recent_joke_flags),
            "last_meta": (
                {"technique": self.last_meta.technique, "topic": self.last_meta.topic}
                if self.last_meta
                else None
            ),
            "weights": learning.describe(self.weights),
            "night": (
                {
                    "date": night.date,
                    "nudges": night.nudges,
                    "last_nudge_ts": (
                        night.last_nudge_ts.isoformat() if night.last_nudge_ts else None
                    ),
                    "declined": night.declined,
                    "last_signals": list(night.last_signals),
                }
                if night
                else None
            ),
            "session_started": self.session_started,
        }

    @classmethod
    def from_blob(cls, blob: dict[str, Any]) -> LiveState:
        raw_weights = blob.get("weights") or {}
        techniques = {
            key: learning.Beta(float(v.get("alpha", 1.0)), float(v.get("beta", 1.0)))
            for key, v in (raw_weights.get("techniques") or {}).items()
            if isinstance(v, dict)
        }
        base = LearningWeights.default().techniques
        base.update(techniques)
        topics = {
            key: learning.Beta(float(v.get("alpha", 1.0)), float(v.get("beta", 1.0)))
            for key, v in (raw_weights.get("topics") or {}).items()
            if isinstance(v, dict)
        }
        rate = raw_weights.get("joke_rate") or {}
        meta = blob.get("last_meta")
        night_raw = blob.get("night")
        night = None
        if isinstance(night_raw, dict):
            ts_raw = night_raw.get("last_nudge_ts")
            night = NightState(
                date=str(night_raw.get("date", "")),
                nudges=int(night_raw.get("nudges", 0)),
                last_nudge_ts=(
                    datetime.fromisoformat(ts_raw) if ts_raw else None
                ),
                declined=bool(night_raw.get("declined", False)),
                last_signals=tuple(night_raw.get("last_signals", []) or []),
            )
        return cls(
            latch=int(blob.get("latch", 0)),
            recent_joke_flags=tuple(blob.get("recent_joke_flags", []) or []),
            last_meta=(
                JokeMeta(technique=meta.get("technique"), topic=meta.get("topic"))
                if isinstance(meta, dict)
                else None
            ),
            weights=LearningWeights(
                techniques=base,
                topics=topics,
                joke_rate=learning.Beta(
                    float(rate.get("alpha", 1.0)), float(rate.get("beta", 1.0))
                ),
            ),
            night=night,
            session_started=blob.get("session_started"),
        )


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def begin_turn(store: Any, key: str) -> LiveState:
    """Load this key's live state (fresh defaults when absent)."""
    load_state = getattr(store, "load_state", None)
    blob = load_state() if callable(load_state) else {}
    state = LiveState.from_blob(blob or {})
    if state.session_started is None:
        state = LiveState(
            latch=state.latch,
            recent_joke_flags=state.recent_joke_flags,
            last_meta=state.last_meta,
            weights=state.weights,
            night=state.night,
            session_started=_utcnow_iso(),
        )
        save_state = getattr(store, "save_state", None)
        if callable(save_state):
            save_state(state.to_blob())
    return state


def extract_stances(reply: str | None, topic: str) -> list[Stance]:
    """Stance sentences in Evie's reply (deterministic markers only)."""
    stances: list[Stance] = []
    for sentence in _SENTENCE_SPLIT.split((reply or "").strip()):
        cleaned = sentence.strip()
        if not cleaned or len(cleaned.split()) < 3:
            continue
        if any(pattern.search(cleaned) for pattern in _STANCE_MARKERS):
            reason = ""
            match = re.search(r"\bbecause\b(.{0,160})", cleaned, re.IGNORECASE)
            if match:
                reason = match.group(1).strip(" .")
            stances.append(
                Stance(topic=topic, text=cleaned[:300], reason=reason, ts=_utcnow_iso())
            )
    return stances


def note_turn(
    store: Any,
    ledger: Any,
    key: str,
    state: LiveState,
    *,
    owner_msg: str,
    prev_reply: str | None,
    prev_meta: JokeMeta | None,
    prev_weights: LearningWeights | None = None,
) -> tuple[LiveState, str]:
    """Learn from the owner's message as a reaction to the previous reply.

    Returns (new_state, reaction). Records landed bits, confirms used
    callbacks, and files stances — all through persona-owned stores.
    """
    reaction = classify_reaction(owner_msg, prev_meta)
    weights = prev_weights if prev_weights is not None else state.weights
    technique = prev_meta.technique if prev_meta else None
    topic = (prev_meta.topic if prev_meta and prev_meta.topic else None) or topic_of(
        owner_msg
    )
    if technique:
        weights = learning.update(weights, technique, topic, reaction)
        if reaction == "positive" and prev_reply:
            first = _SENTENCE_SPLIT.split(prev_reply.strip())
            record_bit = getattr(store, "record_landed_bit", None)
            if first and callable(record_bit):
                record_bit(first[0][:300])
    if prev_reply:
        confirm = getattr(store, "confirm_callback", None)
        suggest = getattr(store, "suggest_callback", None)
        bit = suggest(key) if callable(suggest) else None
        if bit and callable(confirm):
            for sentence in _SENTENCE_SPLIT.split(prev_reply.strip()):
                if bit in sentence:
                    confirm(key, bit)
                    break
    if ledger is not None and prev_reply:
        record = getattr(ledger, "record", None)
        if callable(record):
            for stance in extract_stances(prev_reply, topic):
                record(stance)
    new_state = LiveState(
        latch=state.latch,
        recent_joke_flags=state.recent_joke_flags,
        last_meta=state.last_meta,
        weights=weights,
        night=state.night,
        session_started=state.session_started,
    )
    save_state = getattr(store, "save_state", None)
    if callable(save_state):
        save_state(new_state.to_blob())
    save_weights = getattr(store, "save_weights", None)
    if callable(save_weights):
        save_weights(weights)
    return new_state, reaction


def after_decision(
    store: Any,
    key: str,
    state: LiveState,
    decision: ToneDecision,
    reply: str | None,
    *,
    owner_msg: str = "",
) -> LiveState:
    """Persist post-turn state: latch decay, joke flag, last meta, lines."""
    del key
    latch = update_latch(state.latch, decision)
    flags = tuple([*state.recent_joke_flags, joke_flag(reply, decision)][-3:])
    meta = state.last_meta
    if decision.technique:
        meta = JokeMeta(technique=decision.technique, topic=topic_of(owner_msg))
    new_state = LiveState(
        latch=latch,
        recent_joke_flags=flags,
        last_meta=meta,
        weights=state.weights,
        night=state.night,
        session_started=state.session_started,
    )
    save_state = getattr(store, "save_state", None)
    if callable(save_state):
        save_state(new_state.to_blob())
    if reply:
        record_line = getattr(store, "record_line", None)
        if callable(record_line):
            for sentence in _SENTENCE_SPLIT.split(reply.strip()):
                if len(sentence.split()) >= 4:
                    record_line(sentence[:300])
    return new_state
