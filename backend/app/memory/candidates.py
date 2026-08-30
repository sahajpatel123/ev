"""F5 memory candidate pipeline: value scoring + write/no-write decisions.

Evolution of  Event → Extractor → MemoryWriter : an intelligent decision layer
between extraction and commitment to long-term memory.

PERMANENT LAWS ENFORCED HERE:
  - ASSISTANT-STATEMENT LAW (§14): an assistant statement alone NEVER becomes
    an owner memory. Evie's speculation cannot recursively become truth.
  - SECRET LAW (§16): passwords/keys/tokens/OTPs/credential-shaped content are
    rejected at EXTRACTION time — never stored, never filtered later by prompt.
  - OWNER CORRECTION LAW (§15): a correction supersedes the current version;
    the old row keeps valid_until for as_of history (writer handles rows).
  - WRITE BUDGET (§22): low-value candidates are counted and dropped — the
    event store stays the durable source either way; memory is a curated index.

Modes (EV_MEMORY_SCORING_V2): off | shadow | on.
  off    — legacy pipeline (extractor output is written as before).
  shadow — decisions are computed + counted; nothing changes.
  on     — rejected candidates are dropped before MemoryWriter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.config import settings


class Decision(StrEnum):
    WRITE = "write"
    REJECT_LOW_VALUE = "reject_low_value"
    REJECT_SECRET = "reject_secret"
    REJECT_ASSISTANT_SPECULATION = "reject_assistant_speculation"
    REJECT_DUPLICATE = "reject_duplicate"
    SUPERSEDE = "supersede"


# Candidate classes (§11) mapped onto existing memory types. No new ontology.
CANDIDATE_TYPE_MAP: dict[str, str] = {
    "fact": "fact",
    "preference": "preference",
    "decision": "decision",
    "intention": "goal",
    "goal": "intention",
    "project_progress": "summary",
    "relationship": "fact",
    "personal_event": "episodic",
    "pattern": "pattern",
    "owner_correction": "preference",
    "important_observation": "observation",
}

# §16 SECRET LAW — extraction-time credential rejection.
SECRET_PATTERNS = re.compile(
    r"(?:"
    r"\b(?:password|passwd|pwd|api[-_ ]?key|secret[-_ ]?key|access[-_ ]?token|"
    r"auth[-_ ]?token|bearer|private[-_ ]?key|ssh[-_ ]?key|one[-_ ]?time[-_ ]?code|"
    r"otp|2fa|verification code|recovery code|cvv|card number|api secret)\b"
    r"|\b(?:sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|xox[bap]-[A-Za-z0-9-]{10,})\b"
    r"|\b(?:[0-9]{6})\b(?=.*\b(?:code|otp)\b)"
    r")",
    re.IGNORECASE,
)

# §12 weak-candidate signals (small talk / transient phrasing).
WEAK_PATTERNS = re.compile(
    r"(?:"
    r"^(?:hi|hey|hello|thanks|thank you|ok(?:ay)?|cool|nice|lol|haha|bye)[\s!.?]*$"
    r"|^(?:yes|no|yeah|nope|sure|maybe)[\s!.?]*$"
    r"|\b(?:lol|haha|lmao)\b"
    r")",
    re.IGNORECASE,
)

# §15 owner-correction phrasing.
CORRECTION_PATTERNS = re.compile(
    r"(?:"
    r"\bno,?\s+(?:i\s+)?(?:actually\s+|now\s+)?(?:prefer|like|use|want)\b"
    r"|\b(?:actually|now)\s+i\s+(?:prefer|like|use)\b"
    r"|\bi\s+(?:don'?t|no longer)\s+(?:like|prefer|use)\b"
    r"|\bnot\s+\w+\s+anymore\b.*\b(?:prefer|like|use)\b"
    r")",
    re.IGNORECASE,
)

# §37 intention phrasing — history only, never a schedule.
INTENTION_PATTERNS = re.compile(
    r"\b(?:i\s+(?:should\s+)?(?:probably\s+)?(?:work\s+on|continue|finish|get\s+to)|"
    r"i\s+(?:want|plan|hope|intend)\s+to|tomorrow\s+i\s+should)\b",
    re.IGNORECASE,
)

# Persistence likelihood by base memory type (§13): how long-lived is this
# kind of fact, independent of any single score.
PERSISTENCE_LIKELIHOOD: dict[str, float] = {
    "decision": 0.95,
    "preference": 0.9,
    "fact": 0.85,
    "goal": 0.85,
    "pattern": 0.7,
    "summary": 0.7,
    "lesson": 0.8,
    "observation": 0.45,
    "episodic": 0.4,
}


def scoring_mode() -> str:
    return (getattr(settings, "memory_scoring_v2", "off") or "off").strip().lower()


@dataclass
class CandidateDecision:
    """Deterministic write/no-write decision with machine-readable reason."""

    decision: Decision
    value: float = 0.0
    reason: str = ""
    candidate_class: str | None = None
    correction: bool = False
    components: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "value": round(self.value, 3),
            "reason": self.reason,
            "candidate_class": self.candidate_class,
            "correction": self.correction,
        }


def candidate_class_for(candidate: Any) -> str:
    """Map an extracted MemoryCandidate onto the F5 class ontology."""

    memory_type = str(getattr(candidate, "memory_type", "") or "observation")
    payload = getattr(candidate, "payload", {}) or {}
    if memory_type == "preference" and CORRECTION_PATTERNS.search(
        str(payload.get("topic") or payload.get("text") or "")
    ):
        return "owner_correction"
    if memory_type in CANDIDATE_TYPE_MAP:
        return memory_type
    return "important_observation"


def evaluate_candidate(event: Any, candidate: Any) -> CandidateDecision:
    """Deterministic value/persistence/sensitivity decision for one candidate."""

    text = str(
        (getattr(candidate, "payload", {}) or {}).get("text")
        or (getattr(candidate, "payload", {}) or {}).get("topic")
        or ""
    ).strip()

    # §16 SECRET LAW: fail closed at extraction time.
    if SECRET_PATTERNS.search(text):
        return CandidateDecision(
            decision=Decision.REJECT_SECRET,
            reason="credential_shaped_content",
            candidate_class="secret",
        )

    cclass = candidate_class_for(candidate)

    # §14 ASSISTANT-STATEMENT LAW: the assistant's own words are not owner
    # truth. The durable event remains; no memory row is derived from it.
    event_type = str(getattr(event, "event_type", "") or "")
    if event_type == "message.assistant":
        return CandidateDecision(
            decision=Decision.REJECT_ASSISTANT_SPECULATION,
            reason="assistant_statement_not_owner_memory",
            candidate_class=cclass,
        )

    # §12 weak candidates: small talk / transient noise.
    if WEAK_PATTERNS.search(text) or len(text) < 8:
        return CandidateDecision(
            decision=Decision.REJECT_LOW_VALUE,
            reason="transient_or_trivial",
            candidate_class=cclass,
        )

    # §13 type-aware value scoring (not one magical global weight).
    importance = float(getattr(candidate, "importance", 0.5) or 0.5)
    confidence = float(getattr(candidate, "confidence", 0.8) or 0.8)
    source_type = str(getattr(candidate, "source_type", "") or "inferred")
    explicitness = 1.0 if source_type == "explicit" else 0.6 if source_type == "derived" else 0.4
    persistence = PERSISTENCE_LIKELIHOOD.get(cclass, 0.5)
    novelty = 1.0 if getattr(candidate, "fingerprint", None) else 0.7
    owner_relevance = 1.0 if event_type in {"message.user", "voice"} else 0.6
    value = round(
        0.28 * importance
        + 0.22 * explicitness
        + 0.2 * persistence
        + 0.15 * confidence
        + 0.1 * novelty
        + 0.05 * owner_relevance,
        3,
    )
    components = {
        "importance": importance,
        "explicitness": explicitness,
        "persistence": persistence,
        "confidence": confidence,
        "novelty": novelty,
        "owner_relevance": owner_relevance,
    }

    is_correction = cclass == "owner_correction"
    threshold = 0.32 if not is_correction else 0.0  # corrections always survive
    if value < threshold:
        return CandidateDecision(
            decision=Decision.REJECT_LOW_VALUE,
            reason=f"value_below_threshold({value}<{threshold})",
            value=value,
            candidate_class=cclass,
            correction=is_correction,
            components=components,
        )

    decision = Decision.SUPERSEDE if is_correction else Decision.WRITE
    return CandidateDecision(
        decision=decision,
        value=value,
        reason="correction_supersedes" if is_correction else "durable_candidate",
        candidate_class=cclass,
        correction=is_correction,
        components=components,
    )


# ---------------------------------------------------------------------------
# F5.1 legacy-memory eligibility (read-time calibration — §6 preference)
#
# NOT erasure: the event store and Memory rows are untouched. This is a
# deterministic, bounded, epoch-aware, rebuildable READ-TIME boundary that
# decides which LEGACY rows may enter AUTOMATIC context (shadow memory /
# prospective suggestions). Explicit deep history (recall / search_memory /
# as_of retrieval) is NOT affected — §8 keeps history reachable.
# ---------------------------------------------------------------------------


class Eligibility(StrEnum):
    KEEP_HIGH_VALUE = "keep_high_value"
    KEEP_NORMAL = "keep_normal"
    LOW_VALUE_AUTO_EXCLUDE = "low_value_auto_exclude"
    PROSPECTIVE_EXCLUDE = "prospective_exclude"
    ASSISTANT_SPECULATION = "assistant_speculation"
    SENSITIVE_EXCLUDE = "sensitive_exclude"
    SUPERSEDED = "superseded"
    DUPLICATE = "duplicate"


def legacy_eligibility(memory: Any, *, automatic: bool = True) -> Eligibility:
    """Deterministic read-time eligibility for one Memory row.

    ``automatic=True`` = candidate for AUTOMATIC context (shadow/prospective).
    The classification never deletes anything; explicit history retrieval is
    unaffected (§8).
    """

    memory_type = str(getattr(memory, "memory_type", "") or "observation")
    importance = float(getattr(memory, "importance", 0.5) or 0.5)
    confidence = float(getattr(memory, "confidence", 0.8) or 0.8)
    source_type = str(getattr(memory, "source_type", "") or "inferred")
    privacy = str(getattr(memory, "privacy_level", "") or "normal")
    text = str(getattr(memory, "text", "") or "")

    if not getattr(memory, "is_current", True):
        return Eligibility.SUPERSEDED
    if privacy in {"never_send_to_model", "sensitive"}:
        return Eligibility.SENSITIVE_EXCLUDE
    if SECRET_PATTERNS.search(text):
        return Eligibility.SENSITIVE_EXCLUDE

    # Durable owner-stated classes are always eligible.
    if memory_type in {"decision", "preference", "goal"}:
        return (Eligibility.KEEP_HIGH_VALUE if importance >= 0.8 else Eligibility.KEEP_NORMAL)
    if memory_type in {"fact", "lesson"} and importance >= 0.55:
        return Eligibility.KEEP_NORMAL

    # Episode summaries: high bar for automatic context (chatter residue).
    if memory_type == "summary":
        if importance >= 0.7:
            return Eligibility.KEEP_NORMAL
        return (Eligibility.PROSPECTIVE_EXCLUDE if automatic else Eligibility.KEEP_NORMAL)

    # Observation/episodic: the legacy permissive catch-all — strong filter.
    if memory_type in {"observation", "episodic"}:
        if source_type == "explicit" and importance >= 0.7 and confidence >= 0.7:
            return Eligibility.KEEP_NORMAL
        if importance >= 0.6 and confidence >= 0.6:
            return Eligibility.KEEP_NORMAL
        return Eligibility.LOW_VALUE_AUTO_EXCLUDE

    # Weak inferred material: never automatic, never prospective.
    if source_type == "inferred" and confidence < 0.5:
        return Eligibility.LOW_VALUE_AUTO_EXCLUDE

    return Eligibility.KEEP_NORMAL


def filter_memories_for_automatic(memories: list) -> list:
    """Read-time gate for AUTOMATIC context builders (shadow/prospective)."""

    if scoring_mode() == "off":
        # Scoring off = pre-F5 read behavior; eligibility still guards
        # secrets/superseded (SQL already handles those) — pass through.
        return memories
    return [
        m for m in memories
        if legacy_eligibility(m) in {
            Eligibility.KEEP_HIGH_VALUE,
            Eligibility.KEEP_NORMAL,
        }
    ]


def filter_candidates(event: Any, candidates: list) -> tuple[list, list[CandidateDecision]]:
    """Pipeline stage: decisions for all candidates under the current mode.

    Returns (surviving_candidates, decisions). off → everything survives
    (legacy); shadow → everything survives but decisions are counted; on →
    rejected candidates are dropped before MemoryWriter.
    """

    mode = scoring_mode()
    decisions = [evaluate_candidate(event, c) for c in candidates]
    from app.memory.os_health import note_candidate_decision

    for d in decisions:
        note_candidate_decision(d.decision.value)
    if mode != "on":
        return candidates, decisions
    surviving = [
        c for c, d in zip(candidates, decisions, strict=False)
        if d.decision in {Decision.WRITE, Decision.SUPERSEDE}
    ]
    return surviving, decisions
