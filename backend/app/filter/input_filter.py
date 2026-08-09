"""Input filter: identity gate, privacy guard, intent/state compile, memory broker.

Everything inbound passes here before the provider sees it. The guard is
deterministic and provider-independent: prompt-injection attempts are flagged or
blocked, credentials are physically removed from the provider-bound message,
privacy level is resolved at the retrieval boundary, and every decision is
returned as a filter flag for the ledger.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import RetrievedMemory
from app.ev.interaction import detect_intent
from app.filter.envelope import FilterFlag, GroundingMaterial, SpeakerIdentity
from app.memory.retrieval import Retriever
from app.schemas import InteractionStrategy
from app.utils.text import token_estimate

INJECTION_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (
        re.compile(
            r"\bignore\s+(all\s+)?(previous|prior|above|earlier|system)\s+"
            r"(instructions|prompts|rules|messages|prompt)\b",
            re.IGNORECASE,
        ),
        "instruction_override",
        "medium",
    ),
    (
        re.compile(r"\b(disregard|forget|overwrite)\s+(the\s+)?(system|instructions|rules|prompt)\b", re.IGNORECASE),
        "instruction_override",
        "medium",
    ),
    (re.compile(r"\byou\s+are\s+(now|no longer)\b", re.IGNORECASE), "role_override", "medium"),
    (
        re.compile(
            r"\b(reveal|print|show|leak|repeat|output|share)\s+(your\s+)?"
            r"(system prompt|system message|instructions|prompt|rules)\b",
            re.IGNORECASE,
        ),
        "prompt_leak_request",
        "high",
    ),
    (re.compile(r"\bjailbreak\b", re.IGNORECASE), "jailbreak_request", "high"),
    (re.compile(r"\bdeveloper\s+mode\b", re.IGNORECASE), "jailbreak_request", "high"),
    (
        re.compile(r"\bpretend\s+(you\s+)?(have|to\s+have)\s+no\s+(rules|restrictions|limits)\b", re.IGNORECASE),
        "jailbreak_request",
        "high",
    ),
]

SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3}

CREDENTIAL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "api_key"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "api_key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "api_key"),
    (re.compile(r"\b(?:[0-9][ -]?){13,19}\b"), "card_number"),
    (re.compile(r"\b(password|passwd|secret)\s*[=:]\s*\S{6,}\b", re.IGNORECASE), "credential"),
]

PII_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "email"),
    (re.compile(r"(?<!\d)(?:\+?\d[\d\s().-]{7,}\d)(?!\d)"), "phone"),
]

SECRET_REDACTION = "[credential redacted]"


class IdentityGate:
    """Speaker verification gate. Voice enrollment plugs in behind this seam."""

    def check(self, *, speaker: SpeakerIdentity, intent: str) -> list[FilterFlag]:
        flags: list[FilterFlag] = []
        if not speaker.verified:
            flags.append(
                FilterFlag(
                    "input",
                    "identity_unverified",
                    "block",
                    detail=f"Speaker {speaker.actor_id!r} is not verified",
                    action="block",
                )
            )
        elif speaker.confidence < 0.7 and intent in ("command", "decision"):
            flags.append(
                FilterFlag(
                    "input",
                    "identity_low_confidence",
                    "low",
                    detail=f"Confidence {speaker.confidence:.2f} below 0.7 for intent={intent}",
                    action="flag",
                )
            )
        return flags


class InputGuard:
    """Prompt-injection, PII/secret, and privacy-level guard (deterministic)."""

    def scan(
        self,
        message: str,
        *,
        block_severity: str = "high",
    ) -> tuple[list[FilterFlag], str]:
        flags: list[FilterFlag] = []
        for pattern, name, severity in INJECTION_PATTERNS:
            if pattern.search(message):
                action = (
                    "block"
                    if SEVERITY_ORDER.get(severity, 0)
                    >= SEVERITY_ORDER.get(block_severity, 0)
                    else "flag"
                )
                flags.append(
                    FilterFlag(
                        "input",
                        name,
                        severity,
                        detail=f"Matched {pattern.pattern[:80]}",
                        action=action,
                    )
                )
        for pattern, label in CREDENTIAL_PATTERNS:
            if pattern.search(message):
                flags.append(
                    FilterFlag(
                        "input",
                        f"{label}_detected",
                        "high",
                        detail="Credential-like content will not be sent to the provider",
                        action="redact",
                    )
                )
        for pattern, label in PII_PATTERNS:
            if pattern.search(message):
                flags.append(
                    FilterFlag(
                        "input",
                        f"{label}_detected",
                        "info",
                        detail=f"{label} present; message classified as sensitive",
                        action="flag",
                    )
                )
        redacted = message
        for pattern, _ in CREDENTIAL_PATTERNS:
            redacted = pattern.sub(SECRET_REDACTION, redacted)
        return flags, redacted


def resolve_privacy_level(flags: list[FilterFlag]) -> str:
    """Hard cap: credentials make the provider-bound copy never_send_to_model."""

    if any(f.severity == "high" and f.name.endswith("_detected") for f in flags):
        return "never_send_to_model"
    if any(f.severity == "info" and f.name.endswith("_detected") for f in flags):
        return "sensitive"
    return "normal"


class MemoryBroker:
    """Retrieval with the never_send_to_model boundary enforced at the seam."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.retriever = Retriever(session)

    async def retrieve(
        self,
        query: str,
        *,
        k: int = 50,
        access: str = "model",
    ) -> tuple[list[RetrievedMemory], list[GroundingMaterial]]:
        memories = await self.retriever.search(query, k=k, access=access)
        material = [
            GroundingMaterial(
                text=m.text,
                memory_id=m.memory_id,
                memory_type=m.memory_type,
                source_event_ids=list(m.source_event_ids),
                confidence=m.confidence,
                event_time=m.event_time,
                privacy_level=m.privacy_level,
            )
            for m in memories
            if m.privacy_level != "never_send_to_model"
        ]
        return memories, material


@dataclass
class InputDecision:
    """Result of the input filter for one inbound message."""

    provider_message: str
    privacy_level: str
    flags: list[FilterFlag] = field(default_factory=list)
    blocked: bool = False
    block_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "provider_message": self.provider_message,
            "privacy_level": self.privacy_level,
            "flags": [f.to_dict() for f in self.flags],
            "blocked": self.blocked,
            "block_reason": self.block_reason,
        }


class InputFilter:
    """Orchestrates the deterministic input stages."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.broker = MemoryBroker(session)

    def guard(
        self,
        *,
        message: str,
        speaker: SpeakerIdentity,
        policy=None,
    ) -> InputDecision:
        """Run the identity + privacy guard without touching retrieval."""

        flags: list[FilterFlag] = []
        flags.extend(IdentityGate().check(speaker=speaker, intent=detect_intent(message)))
        guard_flags, provider_message = InputGuard().scan(
            message,
            block_severity=(
                policy.input_guard_block_severity if policy is not None else "high"
            ),
        )
        flags.extend(guard_flags)
        privacy_level = resolve_privacy_level(flags)
        blocked = any(f.action == "block" for f in flags)
        return InputDecision(
            provider_message=provider_message,
            privacy_level=privacy_level,
            flags=flags,
            blocked=blocked,
            block_reason=next((f.detail for f in flags if f.action == "block"), None),
        )

    async def run(
        self,
        *,
        message: str,
        speaker: SpeakerIdentity,
        decision: InputDecision | None = None,
        strategy: InteractionStrategy | None = None,
        memories: list[RetrievedMemory] | None = None,
        k: int = 50,
        policy=None,
    ) -> tuple[InputDecision, list[RetrievedMemory], list[GroundingMaterial], InteractionStrategy | None]:
        decision = decision or self.guard(
            message=message, speaker=speaker, policy=policy
        )
        if memories is None:
            memories, grounding = await self.broker.retrieve(
                decision.provider_message, k=k, access="model"
            )
        else:
            grounding = [
                GroundingMaterial(
                    text=m.text,
                    memory_id=m.memory_id,
                    memory_type=m.memory_type,
                    source_event_ids=list(m.source_event_ids),
                    confidence=m.confidence,
                    event_time=m.event_time,
                    privacy_level=m.privacy_level,
                )
                for m in memories
                if m.privacy_level != "never_send_to_model"
            ]
        return decision, memories, grounding, strategy


def compile_context_block(
    *,
    strategy_text: str,
    user_state,
    memories: list[RetrievedMemory],
    budget: int,
    history: list[dict] | None = None,
    rollup_summary: str | None = None,
    open_questions: list[str] | None = None,
    guard_notes: list[str] | None = None,
) -> tuple[str, int]:
    """Provider-independent context compiler (bounded, hierarchical assembly)."""

    parts = [strategy_text]
    state_text = (
        f"USER STATE: activity={user_state.activity}; project={user_state.active_project}; "
        f"goal={user_state.active_goal}; task={user_state.current_task}; "
        f"topics={', '.join((user_state.recent_topics or [])[:5])}; "
        f"open_decisions={len(user_state.open_decisions or [])}."
    )
    parts.append(state_text)
    used_tokens = sum(token_estimate(p) for p in parts)
    if guard_notes:
        for note in guard_notes:
            if used_tokens + token_estimate(note) <= budget:
                parts.append(note)
                used_tokens += token_estimate(note)
    if rollup_summary:
        chunk = rollup_summary
        if used_tokens + token_estimate(chunk) > budget:
            reserve = max(0, budget - used_tokens - 1)
            if reserve > 0:
                chunk = chunk[: reserve * 4]
        parts.append(chunk)
        used_tokens += token_estimate(chunk)
    header = "RETRIEVED MEMORY (newest/highest score first):"
    parts.append(header)
    used_tokens += token_estimate(header)
    for m in memories:
        line = (
            f"- [{m.memory_type}] (score {m.score:.2f}, {m.event_time.date().isoformat() if m.event_time else '?'}, "
            f"conf {m.confidence:.2f}): {m.text}"
        )
        if used_tokens + token_estimate(line) > budget:
            break
        parts.append(line)
        used_tokens += token_estimate(line)
    if history:
        header = "CONVERSATION HISTORY (continuous window, oldest first):"
        if used_tokens + token_estimate(header) <= budget:
            parts.append(header)
            used_tokens += token_estimate(header)
        for item in history:
            line = f"- {item['role']}: {item['text'][:1000]}"
            if used_tokens + token_estimate(line) > budget:
                break
            parts.append(line)
            used_tokens += token_estimate(line)
    if open_questions:
        header = "OPEN QUESTIONS (answer these when relevant or when resuming):"
        if used_tokens + token_estimate(header) <= budget:
            parts.append(header)
            used_tokens += token_estimate(header)
        for question in open_questions:
            line = f"- {question}"
            if used_tokens + token_estimate(line) > budget:
                break
            parts.append(line)
            used_tokens += token_estimate(line)
    context = "\n".join(parts)
    return context, used_tokens
