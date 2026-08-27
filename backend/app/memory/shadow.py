"""F1 Shadow Memory: retrieval → turn-scoped envelope → exactly-once injection.

Law (F0+F1 directive):
  - History is retrieved ONLY on the final owner transcript, never on PCM.
  - Envelopes are turn-scoped runtime context; never persisted as truth.
  - ``never_send_to_model`` / sandbox scope are enforced at the DATA boundary
    (Retriever access="model" filters at SQL; scope is checked before any
    retrieval happens).
  - Exactly-once: one retrieval + one injection per owner turn, keyed by
    turn_id and query fingerprint (retry/reconnect safe).
  - Modes (EV_MEMORY_GATE): off → no work; shadow → build + measure, never
    inject; on → build and allow injection on the conversation path only.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.memory.foundation import (
    LEVEL_TOKEN_BUDGETS,
    RetrievalClassification,
    RetrievalIntent,
    ShadowItem,
    ShadowMemoryEnvelope,
)
from app.memory.intent import (
    INTENT_RETRIEVAL_CONFIG,
    classify_retrieval,
    semantic_fallback,
    should_escalate_level,
)
from app.memory.observe import log_memory
from app.memory.os_health import (
    note_context_build,
    note_scope_denial,
    note_shadow_classified,
    note_shadow_expired,
    note_shadow_injected,
    note_shadow_retrieval,
    note_shadow_turn,
    note_zero_retrieval_turn,
)
from app.memory.retrieval import Retriever
from app.utils.text import token_estimate

_TURN_TTL = timedelta(seconds=120)
_MAX_TURNS_PER_SCOPE = 16
_MAX_SCOPES = 32

_EXPLICIT_FAMILY = {
    RetrievalIntent.DECISION,
    RetrievalIntent.PAST_EVENT,
    RetrievalIntent.PROJECT_HISTORY,
    RetrievalIntent.TEMPORAL_EXACT,
    RetrievalIntent.FACT,
}


def memory_gate_mode() -> str:
    return (getattr(settings, "memory_gate", "off") or "off").strip().lower()


def query_fingerprint(query: str) -> str:
    return hashlib.sha256((query or "").encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Turn scope registry (process-local, bounded)
# ---------------------------------------------------------------------------


class ShadowTurnScope:
    """Per-live-session turn→envelope map with exactly-once semantics."""

    def __init__(self, session_key: str) -> None:
        self.session_key = session_key
        self.envelopes: dict[str, ShadowMemoryEnvelope] = {}

    def get(self, turn_id: str) -> ShadowMemoryEnvelope | None:
        self._gc()
        envelope = self.envelopes.get(turn_id)
        if envelope is not None and not envelope.expired:
            return envelope
        return None

    def put(self, envelope: ShadowMemoryEnvelope) -> None:
        self._gc()
        self.envelopes[envelope.turn_id] = envelope
        while len(self.envelopes) > _MAX_TURNS_PER_SCOPE:
            oldest = next(iter(self.envelopes))
            self.envelopes.pop(oldest, None)

    def expire_turn(self, turn_id: str) -> bool:
        envelope = self.envelopes.get(turn_id)
        if envelope is None:
            return False
        envelope.expired = True
        note_shadow_expired()
        return True

    def _gc(self) -> None:
        now = datetime.now(UTC)
        stale = [
            turn_id
            for turn_id, envelope in self.envelopes.items()
            if envelope.generated_at is None
            or now - envelope.generated_at > _TURN_TTL
        ]
        for turn_id in stale:
            envelope = self.envelopes.pop(turn_id, None)
            if envelope is not None and not envelope.expired:
                envelope.expired = True
                note_shadow_expired()


_SCOPES: dict[str, ShadowTurnScope] = {}


def scope_for(live_session_id: str | None) -> ShadowTurnScope:
    key = live_session_id or "__default__"
    scope = _SCOPES.get(key)
    if scope is None:
        scope = ShadowTurnScope(key)
        _SCOPES[key] = scope
        while len(_SCOPES) > _MAX_SCOPES:
            oldest = next(iter(_SCOPES))
            _SCOPES.pop(oldest, None)
    return scope


def expire_turn(live_session_id: str | None, turn_id: str) -> bool:
    return scope_for(live_session_id).expire_turn(turn_id)


def mark_injected(envelope: ShadowMemoryEnvelope) -> bool:
    """Exactly-once injection guard. True only on the first call."""

    if envelope.injected or envelope.expired:
        return False
    envelope.injected = True
    note_shadow_injected(envelope.token_count)
    return True


# ---------------------------------------------------------------------------
# Temporal resolution for as_of recall (§28)
# ---------------------------------------------------------------------------


def _resolve_temporal_window(query: str) -> tuple[Any, Any, Any]:
    from app.memory.temporal import resolve_temporal_expressions
    from app.utils.text import utcnow

    try:
        resolved = resolve_temporal_expressions(query, utcnow())
    except Exception:  # noqa: BLE001 - temporal parsing must never break routing
        return None, None, None
    for item in resolved:
        if getattr(item, "start", None) is not None:
            start = item.start
            end = getattr(item, "end", None)
            as_of = (
                start + ((end - start) / 2 if end and end > start else timedelta(0))
                if start is not None
                else None
            )
            return as_of, start, end
    return None, None, None


# ---------------------------------------------------------------------------
# Envelope construction
# ---------------------------------------------------------------------------


async def _search_memories(
    session: AsyncSession,
    request: Any,
    *,
    k: int,
    min_score: float,
    include_historical: bool,
) -> list:
    config = INTENT_RETRIEVAL_CONFIG.get(request.intent, {})
    retriever = Retriever(session)
    return await retriever.search(
        request.query,
        k=k,
        access=request.access,
        include_sensitive=request.include_sensitive,
        as_of=request.as_of if isinstance(request.as_of, datetime) else None,
        memory_types=config.get("memory_types"),
        min_score=min_score,
        include_historical=include_historical,
        weight_overrides=config.get("weight_overrides"),
        rerank=False,
    )


def _items_from_memories(hits: list) -> list[ShadowItem]:
    return [
        ShadowItem(
            text=hit.text[:400],
            memory_type=hit.memory_type,
            score=float(hit.score),
            confidence=float(hit.confidence) if hit.confidence is not None else None,
            importance=float(hit.importance) if hit.importance is not None else None,
            event_time=hit.event_time,
            source_type=hit.source_type,
            ref=hit.memory_id,
            kind="memory",
        )
        for hit in hits
    ]


async def _bootstrap_item() -> ShadowItem | None:
    from app.memory.bootstrap import load_cached_bootstrap

    bootstrap = load_cached_bootstrap() or {}
    fragments: list[str] = []
    if bootstrap.get("active_project"):
        fragments.append(f"active project: {bootstrap['active_project']}")
    if bootstrap.get("last_episode"):
        fragments.append(f"recent focus: {str(bootstrap['last_episode'])[:240]}")
    if bootstrap.get("relationship"):
        fragments.append(f"relationship: {str(bootstrap['relationship'])[:160]}")
    if not fragments:
        return None
    return ShadowItem(
        text="; ".join(fragments),
        memory_type="context",
        kind="bootstrap",
        score=0.5,
    )


async def build_shadow_envelope(
    session: AsyncSession,
    request: Any,
    *,
    classification: RetrievalClassification | None = None,
) -> ShadowMemoryEnvelope | None:
    """Level-appropriate retrieval → envelope. Never persists anything."""

    started = time.perf_counter()
    config = INTENT_RETRIEVAL_CONFIG.get(request.intent, {})
    level = int(request.level or config.get("level", 1))
    if level <= 0 or request.intent in {
        RetrievalIntent.NONE,
        RetrievalIntent.CURRENT_STATE_QUERY,
        RetrievalIntent.UNKNOWN,
    }:
        note_shadow_classified(0, request.intent.value, 0)
        return None

    min_score = float(
        config.get(
            "min_score",
            0.0 if request.intent in _EXPLICIT_FAMILY else 0.18,
        )
    )
    k = int(config.get("k", 6))
    # Superseded/historical rows enter ONLY for explicitly historical intents
    # (as_of recall). Expansion (L2) must never silently mix versions (§11).
    include_historical = bool(config.get("historical", False))

    classify_ms = (time.perf_counter() - started) * 1000.0
    note_shadow_classified(classify_ms, request.intent.value, level)

    t0 = time.perf_counter()
    hits = await _search_memories(
        session, request, k=k, min_score=min_score, include_historical=include_historical
    )
    items = _items_from_memories(hits)
    escalated = 0
    top_score = float(hits[0].score) if hits else 0.0

    # Progressive escalation: expand ONLY when the brief pass under-served (§9).
    if (
        level == 1
        and should_escalate_level(request.intent, top_score, len(items))
    ):
        escalated = 1
        level = 2
        hits = await _search_memories(
            session, request, k=k * 2, min_score=0.0, include_historical=False
        )
        items = _items_from_memories(hits)

    if level >= 2:
        # Add event-timeline provenance lines (bounded).
        retriever = Retriever(session)
        try:
            events = await retriever.search_events(
                request.query, k=4, access=request.access, include_sensitive=False
            )
        except Exception:  # noqa: BLE001 - provenance is best-effort
            events = []
        for event in events:
            items.append(
                ShadowItem(
                    text=str(event.get("text") or "")[:300],
                    memory_type=str(event.get("event_type") or "event"),
                    score=float(event.get("score") or 0.0),
                    ref=event.get("id"),
                    kind="event",
                )
            )

    if config.get("bootstrap") and level == 1:
        bootstrap = await _bootstrap_item()
        if bootstrap is not None:
            items.insert(0, bootstrap)

    if not items:
        note_shadow_retrieval((time.perf_counter() - t0) * 1000.0, 0, 0)
        note_zero_retrieval_turn()
        return None

    envelope = ShadowMemoryEnvelope(
        turn_id=request.turn_id,
        query_fingerprint=query_fingerprint(request.query),
        retrieval_intent=request.intent,
        level=level,
        generated_at=datetime.now(UTC),
        memory_scope=request.memory_scope,
        items=items,
        escalations=escalated,
        diagnosis={
            "candidates": len(items),
            "top_score": round(top_score, 4),
            "escalated": bool(escalated),
            "as_of": request.as_of.isoformat()
            if isinstance(request.as_of, datetime)
            else None,
        },
    )
    rendered = envelope.render(budget_tokens=LEVEL_TOKEN_BUDGETS.get(level, 300))
    envelope.token_count = token_estimate(rendered)
    retrieval_ms = (time.perf_counter() - t0) * 1000.0
    envelope.diagnosis["retrieval_ms"] = round(retrieval_ms, 2)
    note_shadow_retrieval(retrieval_ms, len(items), envelope.token_count)
    log_memory(
        "memory.shadow_envelope",
        extra={
            "turn_id": envelope.turn_id,
            "intent": envelope.retrieval_intent.value,
            "level": envelope.level,
            "items": len(envelope.items),
            "tokens": envelope.token_count,
            "escalated": bool(escalated),
            "fingerprint": envelope.query_fingerprint,
        },
    )
    return envelope


# ---------------------------------------------------------------------------
# Router entry: classify → level → retrieve → (mode-dependent) expose
# ---------------------------------------------------------------------------


async def route_turn(
    session: AsyncSession,
    *,
    query: str,
    turn_id: str,
    live_session_id: str | None = None,
    memory_scope: str = "owner",
    previous_intent: RetrievalIntent | None = None,
    classification: RetrievalClassification | None = None,
) -> ShadowMemoryEnvelope | None:
    """Full F1 pipeline for one final owner turn. Returns None when the turn
    deserves no history (L0/guard/scope denial/mode off)."""

    mode = memory_gate_mode()
    if mode == "off":
        return None

    scope = scope_for(live_session_id)
    existing = scope.get(turn_id)
    if existing is not None:
        # Retry/reconnect: exactly-once retrieval per turn (§20).
        return existing

    started = time.perf_counter()
    if classification is None:
        classification = classify_retrieval(query, previous_intent=previous_intent)
        if classification.intent is RetrievalIntent.NONE and semantic_fallback(query):
            pass  # future: eval-gated semantic fallback (F5)

    note_shadow_turn(classification.intent.value, classification.level)

    if classification.is_current_state_guard:
        # §8: canonical authority answers current-state questions. History may
        # not masquerade as present truth, so the guard yields no envelope.
        note_zero_retrieval_turn()
        log_memory(
            "memory.shadow_guard",
            extra={"turn_id": turn_id, "reason": "current_state_query"},
        )
        return None

    if classification.level <= 0 or classification.intent in {
        RetrievalIntent.NONE,
        RetrievalIntent.UNKNOWN,
    }:
        note_zero_retrieval_turn()
        return None

    if memory_scope != "owner":
        note_scope_denial(memory_scope)
        log_memory(
            "memory.shadow_scope_denied",
            extra={"turn_id": turn_id, "scope": memory_scope},
        )
        return None

    as_of = since = until = None
    if classification.intent is RetrievalIntent.TEMPORAL_EXACT or classification.historical_truth:
        as_of, since, until = _resolve_temporal_window(query)

    from app.memory.foundation import MemoryRetrievalRequest

    request = MemoryRetrievalRequest(
        query=query,
        turn_id=turn_id,
        live_session_id=live_session_id,
        intent=classification.intent,
        level=classification.level,
        memory_scope=memory_scope,
        as_of=as_of,
        since=since,
        until=until,
    )
    envelope = await build_shadow_envelope(session, request, classification=classification)
    context_ms = (time.perf_counter() - started) * 1000.0
    note_context_build(context_ms)
    if envelope is None:
        return None
    if mode == "shadow":
        # Shadow measures what WOULD inject; nothing leaves this function.
        envelope.diagnosis["mode"] = "shadow"
        scope.put(envelope)
        return envelope
    envelope.diagnosis["mode"] = "on"
    scope.put(envelope)
    return envelope


async def iter_scope_sessions() -> AsyncIterator[str]:
    for key in list(_SCOPES):
        yield key
