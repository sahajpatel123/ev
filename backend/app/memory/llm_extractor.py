"""Optional LLM-assisted extraction, fail-closed and never on the hot path.

The rule-based ``Extractor`` is the default and the floor: ingestion writes
rule-derived memories synchronously and never touches a network. This module
implements the asynchronous enrichment pass (Follow-up Order 6): it can route
through the local provider when available or the DeepSeek API, batches calls,
deduplicates by content hash, triages low-value captures away, and is capped by
hard daily/monthly budgets. Output is stored as an immutable ``extraction.llm``
event so a rebuild replays it deterministically.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import ChatMessage, EntityRef, MemoryCandidate, RequestEnvelope
from app.memory.extraction import Extractor
from app.memory.temporal import resolve_temporal_expressions
from app.models import Event
from app.utils.text import fingerprint, normalize_text

ALLOWED_MEMORY_TYPES = {"decision", "preference", "goal", "fact", "observation", "episodic"}
ALLOWED_SOURCE_TYPES = {"explicit", "inferred", "derived"}
ALLOWED_ENTITY_TYPES = {"person", "place", "project", "topic", "other"}
ENRICHMENT_PROVIDERS = {"deepseek", "local"}
TYPED_TYPES = {"decision", "preference", "goal", "fact"}

_OUTPUT_SHAPE = (
    '"candidates":[{"memory_type":"decision|preference|goal|fact|observation|episodic",'
    '"text":"short memory text","importance":0.0-1.0,"confidence":0.0-1.0,'
    '"source_type":"explicit|inferred|derived",'
    '"entities":[{"name":"...","entity_type":"person|place|project|topic|other",'
    '"role":"related"}],"payload":{}}]'
)

SYSTEM_PROMPT = (
    "You are EV's memory extractor. Turn the user capture into typed, "
    "provenanced memory candidates. Respond with ONLY JSON, no prose:\n"
    + _OUTPUT_SHAPE
    + "\n\nRules:\n"
    "- inferred claims must be phrased as observations, never as facts.\n"
    "- Never invent details that are not in the capture.\n"
    "- Do not fabricate confidence; give a real estimate based on the text.\n"
)

BATCH_SYSTEM_PROMPT = (
    "You are EV's memory extractor. Each input line is "
    '"<index> <capture text>". Return ONLY JSON with one result per index:\n'
    '{"results":[{"index":0,"candidates":[{"memory_type":"decision|preference|goal|fact|observation|episodic",'
    '"text":"short memory text","importance":0.0-1.0,"confidence":0.0-1.0,'
    '"source_type":"explicit|inferred|derived",'
    '"entities":[{"name":"...","entity_type":"person|place|project|topic|other",'
    '"role":"related"}],"payload":{}}]}]}\n\n'
    "Rules:\n"
    "- inferred claims must be phrased as observations, never as facts.\n"
    "- Never invent details that are not in the capture.\n"
    "- Do not fabricate confidence; give a real estimate based on the text.\n"
)

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def llm_extraction_enabled() -> bool:
    """Opt-in via env; the rule path is the default either way."""
    return _env_bool("EV_LLM_EXTRACTION_ENABLED", False)


def llm_extraction_sensitive_allowed() -> bool:
    return _env_bool("EV_LLM_EXTRACTION_SENSITIVE", False)


def llm_extraction_batch_size() -> int:
    return max(1, min(50, _env_int("EV_LLM_EXTRACTION_BATCH_SIZE", 8)))


def llm_extraction_daily_call_cap() -> int:
    return max(0, _env_int("EV_LLM_EXTRACTION_DAILY_CALL_CAP", 200))


def llm_extraction_daily_token_cap() -> int:
    return max(0, _env_int("EV_LLM_EXTRACTION_DAILY_TOKEN_CAP", 2_000_000))


def llm_extraction_monthly_call_cap() -> int:
    return max(0, _env_int("EV_LLM_EXTRACTION_MONTHLY_CALL_CAP", 2_000))


def llm_extraction_monthly_token_cap() -> int:
    return max(0, _env_int("EV_LLM_EXTRACTION_MONTHLY_TOKEN_CAP", 20_000_000))


def llm_extraction_tokens_per_call() -> int:
    return max(1, _env_int("EV_LLM_EXTRACTION_TOKENS_PER_CALL", 1_200))


def llm_extraction_cost_per_m_token() -> float:
    """Blended USD/M tokens estimate; override with the real DeepSeek rate."""
    return max(0.0, _env_float("EV_LLM_EXTRACTION_COST_PER_M_TOKEN", 0.50))


def llm_extraction_monthly_captures() -> int:
    """Owner capture rate assumption for the monthly cost estimate."""
    return max(0, _env_int("EV_LLM_EXTRACTION_MONTHLY_CAPTURES", 3_000))


def text_fingerprint(text: str) -> str:
    """Content-hash dedup key: normalized text, so near-identical captures
    (case/whitespace drift) are never re-extracted."""
    return fingerprint({"text": normalize_text(text)})


def should_enrich(event: Event) -> bool:
    """Triage: only send captures the rules did not handle confidently, or
    long/complex ones. Short, clear captures never touch a paid API."""
    privacy = event.privacy_level or "normal"
    if event.event_type == "message.assistant" or privacy == "never_send_to_model":
        return False
    if privacy == "sensitive" and not llm_extraction_sensitive_allowed():
        return False
    text = ((event.content or {}).get("text") or "").strip()
    if not text:
        return False
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    if len(text) >= 100 or len(sentences) > 1:
        return True
    rule_candidates = Extractor().extract(event)
    strong = [
        c
        for c in rule_candidates
        if c.memory_type in TYPED_TYPES
        and c.source_type == "explicit"
        and c.confidence >= 0.9
    ]
    return not strong


def _clean_json(raw: str) -> str:
    block = _JSON_BLOCK_RE.search(raw)
    if block:
        raw = block.group(1)
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object found in model output")
    return raw[start : end + 1]


def _validate_entity(item) -> EntityRef | None:
    if not isinstance(item, dict):
        return None
    name = str(item.get("name") or "").strip()
    if not name:
        return None
    entity_type = str(item.get("entity_type") or "other").strip().lower()
    if entity_type not in ALLOWED_ENTITY_TYPES:
        entity_type = "other"
    role = str(item.get("role") or "related").strip()
    try:
        weight = float(item.get("weight") or 1.0)
    except (TypeError, ValueError):
        weight = 1.0
    return EntityRef(
        name=name,
        entity_type=entity_type,
        role=role or "related",
        weight=max(0.0, min(1.0, weight)),
    )


def _validate_candidate(item, event: Event) -> MemoryCandidate | None:
    if not isinstance(item, dict):
        return None
    memory_type = str(item.get("memory_type") or "").strip().lower()
    if memory_type not in ALLOWED_MEMORY_TYPES:
        return None
    text = str(item.get("text") or "").strip()
    if not text:
        return None
    raw_importance = item.get("importance")
    raw_confidence = item.get("confidence")
    if not isinstance(raw_importance, (int, float)) or not isinstance(
        raw_confidence, (int, float)
    ):
        return None
    importance = float(raw_importance)
    confidence = float(raw_confidence)
    if not (0.0 <= importance <= 1.0 and 0.0 <= confidence <= 1.0):
        return None
    source_type = str(item.get("source_type") or "inferred").strip().lower()
    if source_type not in ALLOWED_SOURCE_TYPES:
        source_type = "inferred"
    payload = item.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    entities = [
        ref
        for raw in (item.get("entities") or [])
        if (ref := _validate_entity(raw)) is not None
    ]
    payload = {
        **payload,
        "llm_extracted": True,
        "model": "deepseek-enrichment",
    }
    temporal = [
        entry.to_dict()
        for entry in resolve_temporal_expressions(text, event.occurred_at)
    ]
    if temporal:
        payload["temporal"] = temporal
    return MemoryCandidate(
        memory_type=memory_type,
        text=text,
        payload=payload,
        importance=round(importance, 3),
        confidence=round(confidence, 3),
        source_type=source_type,
        privacy_level=event.privacy_level or "normal",
        event_time=event.occurred_at,
        entities=entities,
    )


def candidates_to_content(event: Event, candidates: list[MemoryCandidate]) -> dict:
    """Serialize validated candidates into an immutable extraction.llm event."""
    return {
        "source_event_id": str(event.id),
        "candidates": [
            {
                "memory_type": c.memory_type,
                "text": c.text,
                "importance": c.importance,
                "confidence": c.confidence,
                "source_type": c.source_type,
                "payload": c.payload,
                "entities": [
                    {
                        "name": e.name,
                        "entity_type": e.entity_type,
                        "role": e.role,
                        "weight": e.weight,
                    }
                    for e in c.entities
                ],
            }
            for c in candidates
        ],
    }


def candidates_from_content(content: dict, event: Event) -> list[MemoryCandidate]:
    """Rebuild MemoryCandidates from a stored extraction.llm event."""
    candidates: list[MemoryCandidate] = []
    for item in content.get("candidates") or []:
        candidate = _validate_candidate(item, event)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


class LLMExtractor:
    """DeepSeek/local enrichment pass; fails closed when the API is absent."""

    def __init__(self, provider=None, session: AsyncSession | None = None) -> None:
        self.provider = provider
        self.session = session
        self.last_error: str | None = None

    @property
    def available(self) -> bool:
        if not llm_extraction_enabled():
            return False
        if self.provider is None:
            from app.gateway.providers import get_chat_provider

            self.provider = get_chat_provider()
        return (
            self.provider is not None
            and getattr(self.provider, "name", "") in ENRICHMENT_PROVIDERS
        )

    async def _call(
        self,
        messages: list[ChatMessage],
    ):
        """One audited provider call; failures are recorded, never raised."""
        from app.gateway.service import ModelGateway

        envelope = RequestEnvelope(
            request_id=str(uuid4()),
            strategy={"mode": "memory_extraction"},
            metadata={"purpose": "llm-extraction", "provider": getattr(self.provider, "name", "?")},
        )
        try:
            gateway = ModelGateway(self.provider)
            call = await gateway.chat(messages, envelope=envelope, temperature=0.0)
        except Exception as exc:  # noqa: BLE001 - enrichment is best-effort
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None
        if call.status != "ok":
            self.last_error = call.error or call.status
            return None
        if self.session is not None:
            from app.services.model_call import log_model_call

            with suppress(Exception):
                await log_model_call(self.session, call=call, actor="memory")
        return call.result.text

    @staticmethod
    def _eligible(event: Event) -> bool:
        privacy = event.privacy_level or "normal"
        if event.event_type == "message.assistant":
            return False
        if privacy == "never_send_to_model":
            return False
        if privacy == "sensitive" and not llm_extraction_sensitive_allowed():
            return False
        return bool(((event.content or {}).get("text") or "").strip())

    async def extract(self, event: Event) -> list[MemoryCandidate] | None:
        """One-event extraction; ``None`` means the brain is unavailable."""
        self.last_error = None
        if not self.available:
            return None
        if not self._eligible(event):
            return []
        text = ((event.content or {}).get("text") or "").strip()
        messages = [
            ChatMessage(role="system", content=SYSTEM_PROMPT),
            ChatMessage(
                role="user",
                content=f"Capture (occurred {event.occurred_at.isoformat()}):\n{text}",
            ),
        ]
        raw = await self._call(messages)
        if raw is None:
            return []
        try:
            data = json.loads(_clean_json(raw))
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"parse: {exc}"
            return []
        if not isinstance(data, dict):
            return []
        return [
            candidate
            for item in data.get("candidates") or []
            if (candidate := _validate_candidate(item, event)) is not None
        ]

    async def extract_batch(
        self,
        events: Sequence[Event],
    ) -> list[tuple[Event, list[MemoryCandidate]]]:
        """Batched extraction (one API call per chunk, indexed results)."""
        eligible = [event for event in events if self._eligible(event)]
        if not eligible or not self.available:
            return []
        payload = "\n\n".join(
            f"[{i}] {((event.content or {}).get('text') or '').strip()}"
            for i, event in enumerate(eligible)
        )
        messages = [
            ChatMessage(role="system", content=BATCH_SYSTEM_PROMPT),
            ChatMessage(role="user", content=payload),
        ]
        raw = await self._call(messages)
        if raw is None:
            # Fall back to per-event calls rather than dropping the batch.
            output: list[tuple[Event, list[MemoryCandidate]]] = []
            for event in eligible:
                candidates = await self.extract(event)
                if candidates:
                    output.append((event, candidates))
            return output
        try:
            data = json.loads(_clean_json(raw))
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"parse: {exc}"
            return []
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            return []
        by_index = {int(r.get("index", -1)): r.get("candidates") or [] for r in results}
        output = []
        for i, event in enumerate(eligible):
            candidates = [
                candidate
                for item in by_index.get(i, [])
                if (candidate := _validate_candidate(item, event)) is not None
            ]
            if candidates:
                output.append((event, candidates))
        return output


def measure_enrichment_economics(captures: Sequence[dict], *, batch_size: int | None = None) -> dict:
    """Calls-per-100-captures estimate from triage + batching over capture text."""
    batch_size = batch_size or llm_extraction_batch_size()
    enriched = 0
    for capture in captures:
        event = Event(
            source="eval",
            event_type="note",
            content={"text": capture["text"]},
            occurred_at=datetime(2026, 8, 12, 9, 30, tzinfo=UTC),
            sha256="0" * 64,
        )
        if should_enrich(event):
            enriched += 1
    calls = (enriched + batch_size - 1) // batch_size if enriched else 0
    captures_count = len(captures)
    calls_per_100 = round(calls / captures_count * 100, 2) if captures_count else 0.0
    monthly_captures = llm_extraction_monthly_captures()
    calls_per_month = round(calls_per_100 / 100 * monthly_captures)
    tokens_per_month = calls_per_month * llm_extraction_tokens_per_call()
    monthly_cost = round(
        tokens_per_month * llm_extraction_cost_per_m_token() / 1_000_000, 2
    )
    return {
        "captures": captures_count,
        "enriched": enriched,
        "triage_skip": captures_count - enriched,
        "api_calls": calls,
        "calls_per_100_captures": calls_per_100,
        "batch_size": batch_size,
        "monthly_assumptions": {
            "captures_per_month": monthly_captures,
            "tokens_per_call": llm_extraction_tokens_per_call(),
            "cost_per_m_token_usd": llm_extraction_cost_per_m_token(),
        },
        "estimated_calls_per_month": calls_per_month,
        "estimated_tokens_per_month": tokens_per_month,
        "estimated_monthly_cost_usd": monthly_cost,
    }


async def replay_llm_extraction_event(session: AsyncSession, event: Event) -> int:
    """Deterministically derive memories from a stored extraction.llm event."""
    from sqlalchemy import select

    from app.memory.writer import MemoryWriter
    from app.models import MemoryEvent as MemoryEventModel

    content = event.content or {}
    source_event_id = content.get("source_event_id")
    candidates = candidates_from_content(content, event)
    if not candidates:
        return 0

    # Skip candidates that duplicate the deterministic rule-based path for the
    # same source event: the LLM pass refines, it does not echo.
    rule_texts: set[tuple[str, str]] = set()
    source: Event | None = None
    if source_event_id:
        source = (
            await session.execute(
                select(Event).where(Event.id == UUID(str(source_event_id)))
            )
        ).scalar_one_or_none()
        if source is not None:
            rule_texts = {
                (c.memory_type, normalize_text(c.text).rstrip("."))
                for c in Extractor().extract(source)
            }
    candidates = [
        c
        for c in candidates
        if (c.memory_type, normalize_text(c.text).rstrip(".")) not in rule_texts
    ]
    if not candidates:
        return 0

    writer = MemoryWriter(session)
    written = await writer.write_all(event, candidates)
    if source is not None:
        memory_ids = [w.memory_id for w in written]
        existing_rows = (
            await session.execute(
                select(MemoryEventModel).where(MemoryEventModel.event_id == source.id)
            )
        ).scalars().all()
        existing = {(row.memory_id, row.event_id) for row in existing_rows}
        for memory_id in memory_ids:
            row = MemoryEventModel(memory_id=UUID(memory_id), event_id=source.id)
            if (UUID(memory_id), source.id) not in existing:
                session.add(row)
    return len(written)
