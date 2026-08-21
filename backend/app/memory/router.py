"""Fast deterministic MemoryRouter. No DeepSeek on the live path.

Modes: fresh, continuation, implicit, explicit, historical.
Default EV_MEMORY_GATE=off. shadow records what would inject. on still does
not change OpenAI create_response; it only feeds bootstrap / search_memory.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.ev.continuity import classify_memory_intent
from app.memory.bootstrap import get_bootstrap, load_cached_bootstrap
from app.memory.index import lookup_entities
from app.memory.os_health import note_gate_ms
from app.memory.recall import build_explicit_recall_payload

_FRESH_HINTS = ("weather", "time", "timer", "volume", "mute")


def _query_fp(query: str) -> str:
    return hashlib.sha256((query or "").encode("utf-8")).hexdigest()[:12]


def _mode_for(query: str, intent: str) -> str:
    lowered = (query or "").lower()
    if intent == "explicit_recall":
        if any(word in lowered for word in ("originally", "before", "used to", "last week", "yesterday")):
            return "historical"
        return "explicit"
    if intent == "fresh":
        return "fresh"
    if intent == "continuation":
        return "continuation"
    if any(hint in lowered for hint in _FRESH_HINTS) and intent == "fresh":
        return "fresh"
    return "implicit"


async def select_context(session: AsyncSession, query: str, *, k: int = 8) -> dict[str, Any]:
    started = time.perf_counter()
    intent = classify_memory_intent(query)
    mode = _mode_for(query, intent)
    packet: dict[str, Any] = {
        "mode": mode,
        "intent": intent,
        "query_fp": _query_fp(query),
        "recent_context": None,
        "relevant_memories": [],
        "relevant_project": None,
        "relevant_episode": None,
        "historical_evidence": [],
        "would_inject": False,
        "grounding": "none",
    }
    bootstrap = load_cached_bootstrap() or await get_bootstrap(session)
    if mode == "fresh":
        packet["retrieval_ms"] = round((time.perf_counter() - started) * 1000, 2)
        note_gate_ms(packet["retrieval_ms"])
        return packet
    packet["relevant_project"] = bootstrap.get("active_project")
    packet["relevant_episode"] = bootstrap.get("last_episode")
    if mode in {"explicit", "historical"}:
        payload = await build_explicit_recall_payload(session, query, k=k)
        evidence = payload.get("evidence") or []
        packet["historical_evidence"] = evidence
        packet["grounding"] = payload.get("grounding") or "none"
        packet["would_inject"] = bool(evidence)
    elif mode == "continuation":
        packet["recent_context"] = bootstrap.get("relationship")
        packet["would_inject"] = bool(bootstrap.get("relationship"))
    else:
        entities = await lookup_entities(session, query, k=4)
        packet["relevant_memories"] = [
            {"name": entity.name, "entity_type": entity.entity_type} for entity in entities
        ]
        packet["would_inject"] = bool(entities) or bool(bootstrap.get("active_project") and query)
        if packet["would_inject"] and not entities:
            # Project-card routing without dumping history onto a weak match.
            packet["recent_context"] = (bootstrap.get("last_episode") or "")[:240]
    packet["retrieval_ms"] = round((time.perf_counter() - started) * 1000, 2)
    note_gate_ms(packet["retrieval_ms"])
    return packet


async def observe_turn(session: AsyncSession, query: str) -> dict[str, Any] | None:
    """Shadow/on gate. Never raises into the persist path. Does not delay PCM."""

    mode = (settings.memory_gate or "off").strip().lower()
    if mode not in {"shadow", "on"}:
        return None
    try:
        packet = await select_context(session, query)
    except Exception:  # noqa: BLE001
        return None
    from app.memory.observe import log_memory

    log_memory(
        "memory.gate_shadow",
        extra={
            "mode": packet.get("mode"),
            "intent": packet.get("intent"),
            "query_fp": packet.get("query_fp"),
            "retrieval_ms": packet.get("retrieval_ms"),
            "would_inject": packet.get("would_inject"),
            "evidence": len(packet.get("historical_evidence") or []),
            "gate": mode,
        },
    )
    return packet
