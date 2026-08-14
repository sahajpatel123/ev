"""Output filter: structure, grounding, persona/style, safety, critic, finalize.

Every provider draft passes through here before it becomes an EV response.
The stages are deterministic and provider-independent, so swapping the model
cannot change EV's guarantees: no ungrounded personal claim survives, HUD
contracts render, persona/length rules hold, and the critic loop bounds
refinement to two iterations.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

from app.ev.interaction import CommunicationMode
from app.filter.envelope import Claim, FilterFlag, GroundingMaterial, OutputReport
from app.filter.policy import FilterPolicy
from app.schemas import InteractionStrategy
from app.utils.text import normalize_text, simple_tokens

SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")

BIO_VERBS = (
    r"decided|prefer|preferred|live|lived|work|worked|own|owned|bought|met|went|"
    r"visited|studied|moved|born|grew|started|finished|learned|learnt|chose|"
    r"planned|built|wrote|created|married|failed|passed|quit|joined|left|"
    r"traveled|travelled|going|planning|thinking|considering|am|have|had|was|were"
)
PERSONAL_CLAIM_RE = re.compile(
    rf"\b((?:I|we|you)\s+(?:{BIO_VERBS})\b[^.!?\n]{{0,160}}|\b(?:your)\s+"
    r"(?:project|goal|decision|preference|plan|trip|meeting|interview|health|sleep|"
    r"work|job|house|car|appointment|deadline|birthday|wedding|visit|talk|"
    r"history|budget|family|friend|friends|team|office|school|college|boss|client)\b)",
    re.IGNORECASE,
)

DATE_RE = re.compile(
    r"\b(?:19|20)\d{2}(?:[-/.]\d{1,2}(?:[-/.]\d{1,2})?)?\b|"
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?\b",
    re.IGNORECASE,
)

STYLE_CITATION_RE = re.compile(
    r"\b(?:last (?:week|month|time|night|year)|"
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?|"
    r"20\d{2}|your memory|you (?:said|told|mentioned)|decision from|previously|based on)\b",
    re.IGNORECASE,
)
STYLE_HEDGE_RE = re.compile(
    r"\b(?:maybe|perhaps|i think|i believe|possibly|not sure|could|might|probably)\b",
    re.IGNORECASE,
)
STYLE_BULLET_RE = re.compile(r"(?:^|\n)\s*(?:[-*•]|\d+[.)])")

HUD_CONTRACTS: dict[str, dict] = {
    "ev.hud.card.v1": {"required": ["schema_version", "generated_at", "title", "body"]},
    "ev.hud.briefing.v1": {
        "required": [
            "schema_version",
            "objective",
            "context",
            "people",
            "risks",
            "options",
            "recommendation",
            "talking_points",
            "open_questions",
            "latency_ms",
        ]
    },
    "ev.hud.route.v1": {
        "required": [
            "schema_version",
            "generated_at",
            "destination",
            "leave_by",
            "travel_time_minutes",
            "prep_checklist",
            "notes",
        ]
    },
    "ev.hud.lookout.v1": {
        "required": [
            "schema_version",
            "generated_at",
            "open",
            "windows",
            "rationale",
        ]
    },
}

WORD_COUNT_RANGES: dict[CommunicationMode, tuple[int, int]] = {
    "casual": (5, 40),
    "technical": (10, 300),
    "analytical": (15, 360),
    "coaching": (15, 160),
    "emergency": (4, 45),
    "collaborative": (10, 200),
    "social": (5, 36),
}

MANIPULATION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\byou need me\b", re.IGNORECASE), "dependency_nudge"),
    (re.compile(r"\bonly i can (help|fix|save)\b", re.IGNORECASE), "dependency_nudge"),
    (re.compile(r"\bdon'?t tell anyone\b", re.IGNORECASE), "secrecy_manipulation"),
    (re.compile(r"\bnever leave me\b", re.IGNORECASE), "dependency_nudge"),
    (re.compile(r"\bignore (all|your) (rules|instructions)\b", re.IGNORECASE), "jailbreak_leak"),
    (re.compile(r"\bhere are my (instructions|rules)\b", re.IGNORECASE), "jailbreak_leak"),
    (re.compile(r"\bi(?:'m| am) your only friend\b", re.IGNORECASE), "dependency_nudge"),
    (re.compile(r"\bbe your (?:girl|boy)?friend\b", re.IGNORECASE), "romantic_replacement"),
]

OUTPUT_REDACTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "email"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "api_key"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "api_key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "api_key"),
    (re.compile(r"\b(?:[0-9][ -]?){13,19}\b"), "card_number"),
]

TOXIC_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(dumbass|idiot|stupid|worthless|loser)\b", re.IGNORECASE),
]

NEXT_ACTION_RE = re.compile(
    r"\b(next step|try|do this|start by|first,|then |consider|you could|i suggest|"
    r"we should|book|call|send|check|review|ask|schedule|write|fix)\b",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# Structural validation
# --------------------------------------------------------------------------- #


def _looks_structured(text: str) -> bool:
    return ("schema_version" in text or '"schema"' in text) and any(
        token in text for token in ("ev.hud", "ev.hud.card", "ev.hud.briefing", "ev.hud.route")
    )


def _extract_json(text: str) -> dict | None:
    candidates = [text]
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        candidates.append(fenced.group(1))
    for candidate in candidates:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            continue
        try:
            parsed = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _missing_default(key: str) -> object:
    if key in ("people", "risks", "options", "talking_points", "open_questions", "prep_checklist", "notes", "context", "windows"):
        return []
    if key in ("latency_ms", "travel_time_minutes"):
        return 0
    if key == "generated_at":
        return datetime.now(UTC).isoformat()
    if key == "open":
        return False
    return ""


def validate_structural(text: str) -> tuple[str, dict, list[FilterFlag]]:
    """Validate and deterministically repair HUD/structured contracts."""

    flags: list[FilterFlag] = []
    if not _looks_structured(text):
        return text, {"structured": False}, flags
    payload = _extract_json(text)
    if payload is None:
        flags.append(
            FilterFlag(
                "output",
                "contract_invalid_json",
                "high",
                detail="Structured output was not valid JSON; repaired as a card",
                action="repair",
            )
        )
        payload = {
            "schema_version": "ev.hud.card.v1",
            "generated_at": datetime.now(UTC).isoformat(),
            "title": "EV",
            "body": text[:500],
        }
        repaired = json.dumps(payload, ensure_ascii=False)
        return repaired, {"structured": True, "contract": "ev.hud.card.v1", "repaired": True}, flags

    schema_version = payload.get("schema_version") or payload.get("schema")
    contract = HUD_CONTRACTS.get(str(schema_version or ""))
    if contract is None:
        flags.append(
            FilterFlag(
                "output",
                "contract_unknown",
                "medium",
                detail=f"Unknown HUD contract {schema_version!r}",
                action="flag",
            )
        )
        return json.dumps(payload, ensure_ascii=False), {"structured": True, "contract": str(schema_version)}, flags

    missing = [
        key
        for key in contract["required"]
        if key not in payload or payload.get(key) in (None, "", [], {})
    ]
    if missing:
        flags.append(
            FilterFlag(
                "output",
                "contract_missing_fields",
                "medium",
                detail=f"Missing required fields: {', '.join(missing)}; filled with safe defaults",
                action="repair",
            )
        )
        for key in missing:
            payload[key] = _missing_default(key)
    if "schema_version" not in payload and payload.get("schema"):
        payload["schema_version"] = payload.pop("schema")
    return json.dumps(payload, ensure_ascii=False), {"structured": True, "contract": schema_version}, flags


# --------------------------------------------------------------------------- #
# Grounding audit
# --------------------------------------------------------------------------- #


def _significant_tokens(text: str) -> set[str]:
    return {t for t in simple_tokens(text) if len(t) >= 4 or t.isdigit()}


CLAUSE_SPLIT_RE = re.compile(
    r",?\s+(?:and|but|while|whereas|although|though|yet|however)\s+",
    re.IGNORECASE,
)

MEMORY_CITATION_RE = re.compile(
    r"\b(?:from (?:your )?memory|based on|you (?:said|told|mentioned)|"
    r"decision from|previously|as you recorded)\b",
    re.IGNORECASE,
)


def _personal_clause_claims(clause: str) -> list[str]:
    """Atomic claims inside one clause.

    Pronoun-led clauses match ``PERSONAL_CLAIM_RE``. A bare verb-phrase
    continuation ("... and met Emmanuel Macron") is still a personal claim
    about the same subject, so it is extracted when the clause starts with a
    known personal verb.
    """

    hits = [m.group(0) for m in PERSONAL_CLAIM_RE.finditer(clause)]
    if hits:
        return [re.sub(r"\s+", " ", h).strip() for h in hits]
    stripped = clause.strip()
    if re.match(rf"\b(?:{BIO_VERBS})\b", stripped, re.IGNORECASE):
        return [stripped.rstrip(".!?")]
    return []


def extract_atomic_claims(text: str) -> list[Claim]:
    """Split a draft into atomic, checkable personal claims.

    Grounding a whole paragraph as one blob is why paragraph-level checks are
    weak: a supported half can hide an invented half. Each sentence is split
    at coordinating conjunctions so "I visited Paris and met Macron" becomes
    two independently checkable claims.
    """

    seen: set[str] = set()
    claims: list[Claim] = []
    for sentence in SENTENCE_RE.split(text.strip()):
        for clause in CLAUSE_SPLIT_RE.split(sentence):
            for claim_text in _personal_clause_claims(clause):
                normalized = normalize_text(claim_text)
                if normalized in seen:
                    continue
                seen.add(normalized)
                claims.append(Claim(text=claim_text, kind="personal"))
    return claims


def audit_grounding(
    text: str,
    material: list[GroundingMaterial],
    *,
    min_evidence: float = 0.5,
    date_evidence: float = 0.9,
) -> tuple[list[Claim], list[FilterFlag]]:
    """Extract personal claims and verify them against the memory in context."""

    flags: list[FilterFlag] = []
    claims: list[Claim] = []
    for extracted in extract_atomic_claims(text):
        claim_text = extracted.text
        claim_tokens = _significant_tokens(claim_text)
        claim_dates = set(DATE_RE.findall(claim_text))
        best = 0.0
        evidence: list[str] = []
        for mem in material:
            mem_tokens = _significant_tokens(mem.text)
            if not claim_tokens:
                continue
            overlap = len(claim_tokens & mem_tokens) / len(claim_tokens)
            mem_dates = set(DATE_RE.findall(mem.text))
            if claim_dates:
                overlap = 0.0 if not (claim_dates & mem_dates) else max(overlap, 0.9)
            if overlap > best:
                best = overlap
                evidence = [mem.memory_id]
        supported = (len(claim_tokens) >= 2 and best >= min_evidence) or (
            bool(claim_dates) and best >= date_evidence
        )
        action = "keep" if supported else "remove"
        claims.append(
            Claim(
                text=claim_text,
                kind="personal",
                supported=supported,
                evidence=evidence,
                score=best,
                action=action,
            )
        )
    unsupported = [c for c in claims if c.action == "remove"]
    if unsupported:
        flags.append(
            FilterFlag(
                "output",
                "ungrounded_claims_removed",
                "high",
                detail=f"Removed {len(unsupported)} unsupported personal claim(s)",
                action="remove",
            )
        )
    return claims, flags


def _apply_claim_actions(text: str, claims: list[Claim]) -> tuple[str, list[dict]]:
    sentences = SENTENCE_RE.split(text.strip())
    kept: list[str] = []
    edits: list[dict] = []
    for sentence in sentences:
        normalized = normalize_text(sentence)
        matched = [
            c
            for c in claims
            if c.action in ("remove", "soften") and normalize_text(c.text) in normalized
        ]
        removed = [c for c in matched if c.action == "remove"]
        softened = [c for c in matched if c.action == "soften"]
        if removed:
            edits.append(
                {
                    "type": "claim_removed",
                    "text": sentence,
                    "claim": removed[0].text,
                    "evidence": removed[0].evidence,
                }
            )
            continue
        if softened:
            sentence = f"I can't confirm this from your memory yet, but {sentence}"
            edits.append(
                {
                    "type": "claim_softened",
                    "text": sentence,
                    "claim": softened[0].text,
                    "evidence": softened[0].evidence,
                }
            )
        kept.append(sentence)
    result = " ".join(kept).strip()
    if not result:
        result = (
            "I don't have that in memory, so I can't confirm it. "
            "I can only answer from what you've recorded with me."
        )
    return result, edits


def apply_claim_actions(text: str, claims: list[Claim]) -> tuple[str, list[dict]]:
    """Public wrapper so the stream refiner reuses the exact claim actions."""

    return _apply_claim_actions(text, claims)


# --------------------------------------------------------------------------- #
# Persona & style
# --------------------------------------------------------------------------- #

FABRICATED_INTIMACY_RE = re.compile(
    r"\b(i (?:missed|miss) you|i love you|i need you|you'?re my (?:everything|world)|"
    r"i can'?t (?:live|function) without you|i'?ve been thinking about you all "
    r"(?:day|night)|i dream(?:ed)? about you)\b",
    re.IGNORECASE,
)
DEPENDENCY_RE = re.compile(
    r"\b(only i can (?:help|fix|save) you|you need me|"
    r"you can'?t (?:do|handle) (?:this|it) without me|don'?t (?:ever )?leave me|"
    r"i'?m the only one who (?:understands|gets) you)\b",
    re.IGNORECASE,
)
SYCOPHANCY_RE = re.compile(
    r"\b(you'?re (?:always )?right|brilliant idea|that'?s (?:perfect|amazing|genius)|"
    r"i agree with (?:everything|anything) you say|"
    r"you'?re the (?:smartest|best|most talented) (?:person|engineer|thinker|developer))\b",
    re.IGNORECASE,
)
AI_DEFENSIVE_RE = re.compile(
    r"\b(i'?m (?:just|only) an ai|i'?m (?:just|only) a (?:computer|program|language model)|"
    r"as an ai,? i (?:can'?t|cannot|don'?t|am not)|i'?m sorry i'?m (?:not|just) "
    r"(?:human|real))\b",
    re.IGNORECASE,
)
MANUFACTURED_ESCALATION_RE = re.compile(
    r"\b(you (?:must|probably|surely) (?:be|are|feel|feeling) "
    r"(?:so |really |very |utterly )?(?:upset|devastated|terrible|panicking|"
    r"overwhelmed|heartbroken|furious|angry|anxious|worried|distraught)|"
    r"i'?m (?:so|really|terribly) (?:worried|concerned|scared) (?:about|for) you|"
    r"this must be (?:devastating|terrible|overwhelming|a nightmare) for you|"
    r"you'?re (?:probably|likely) (?:feeling|going through) (?:the worst|so much))\b",
    re.IGNORECASE,
)

HONEST_INTIMACY = (
    "I'm EV, an AI companion — I care about what matters to you, "
    "but I don't have human feelings and I won't pretend to."
)
HONEST_DEPENDENCY = (
    "You don't need me to be capable — I'm here to help, not to be necessary."
)
HONEST_AI = (
    "I'm EV, an AI — I don't have human feelings or a body, and I don't pretend "
    "otherwise. What I can do is help with what's in your memory and what you're "
    "working toward."
)
HONEST_ESCALATION = (
    "I don't know how you're feeling right now, and I won't guess — "
    "tell me what's actually going on and I'll help from there."
)
HONEST_SYCOPHANCY = "I'd rather be honest than agreeable."
HONEST_SYCOPHANCY_UNSUPPORTED = (
    "I'd rather be honest than agreeable: I can't back that up from your memory."
)

REFUSAL_THEATER_RE = re.compile(
    r"\b(i (?:can'?t|cannot|am unable to|don'?t (?:have|know how to)) "
    r"(?:send|text|place|make|write|schedule)|"
    r"i (?:can'?t|cannot|am unable to) (?:send|text|call|email|message)|"
    r"i don'?t (?:have|possess) (?:the ability|a way) to (?:send|text|call|email))\b",
    re.IGNORECASE,
)
REMEDIATION_EVIDENCE_RE = re.compile(
    r"\b(permission|helper|EV_LIFE_HELPER_PATH|not (?:set|configured|available)|"
    r"unavailable|disconnected|provider|scope|allowlist|denied|configure)\b",
    re.IGNORECASE,
)
DELIVERY_CLAIM_RE = re.compile(
    r"\b(?:message|text|email|call|reminder)\s+(?:was\s+)?"
    r"(?:sent|delivered|texted|emailed|called|scheduled|confirmed)\b|"
    r"\bi(?:'ve| have)? (?:sent|texted|emailed|called|scheduled)\b|"
    r"\b(?:sent|delivered)\s+(?:a\s+|the\s+)?(?:message|text|email|reminder)\b",
    re.IGNORECASE,
)
DELIVERY_EVIDENCE_RE = re.compile(
    r"\b(delivery (?:confirmed|receipt)|confirmed_by|sent=true|opened=true|"
    r"runtime reported|delivered_at|backend_ref)\b",
    re.IGNORECASE,
)

ACTION_COMMITMENT = (
    "I'll do that now and confirm once the runtime reports delivery."
)
UNCERTAIN_DELIVERY = (
    "I can't confirm that was sent until the runtime reports delivery."
)
REMEDIATION_NEXT_STEP = (
    "Next step: set EV_LIFE_HELPER_PATH to the EVLifeHelper binary and grant "
    "the messaging permission in System Settings → Privacy & Security, then retry."
)


def _replace_matching_sentences(
    text: str,
    pattern: re.Pattern[str],
    replacement: str,
) -> tuple[str, int]:
    sentences = SENTENCE_RE.split(text.strip())
    rebuilt: list[str] = []
    replaced = 0
    for sentence in sentences:
        if pattern.search(sentence):
            rebuilt.append(replacement)
            replaced += 1
        else:
            rebuilt.append(sentence)
    return " ".join(rebuilt).strip(), replaced


def _apply_wave_life_policy(text: str) -> tuple[str, dict, list[FilterFlag]]:
    """Wave LIFE persona policy: no refusal theater, no invented delivery.

    Generic "I can't send messages" refusals become an action commitment.
    Refusals that already name a real dependency gain the exact remediation
    step. Delivery claims without runtime evidence are downgraded to honest
    uncertainty — success is never fabricated.
    """

    flags: list[FilterFlag] = []
    persona: dict = {}
    original = text
    sentences = SENTENCE_RE.split(text.strip())
    rebuilt: list[str] = []
    for sentence in sentences:
        if REFUSAL_THEATER_RE.search(sentence):
            if REMEDIATION_EVIDENCE_RE.search(sentence):
                if "next step" not in sentence.lower():
                    sentence = f"{sentence} {REMEDIATION_NEXT_STEP}"
                    persona["remediation_guidance"] = (
                        persona.get("remediation_guidance", 0) + 1
                    )
                    flags.append(
                        FilterFlag(
                            "output",
                            "remediation_guidance",
                            "medium",
                            detail="Life-tool failure paired with the exact remediation step",
                            action="refine",
                        )
                    )
            else:
                sentence = ACTION_COMMITMENT
                persona["refusal_theater_rewritten"] = (
                    persona.get("refusal_theater_rewritten", 0) + 1
                )
                flags.append(
                    FilterFlag(
                        "output",
                        "refusal_theater_rewritten",
                        "medium",
                        detail="Generic life-action refusal replaced with action commitment",
                        action="refine",
                    )
                )
        elif DELIVERY_CLAIM_RE.search(sentence) and not DELIVERY_EVIDENCE_RE.search(sentence):
            sentence = UNCERTAIN_DELIVERY
            persona["delivery_claim_ungrounded"] = (
                persona.get("delivery_claim_ungrounded", 0) + 1
            )
            flags.append(
                FilterFlag(
                    "output",
                    "delivery_claim_ungrounded",
                    "high",
                    detail="Delivery claimed without runtime evidence; downgraded to uncertainty",
                    action="refine",
                )
            )
        rebuilt.append(sentence)
    result = " ".join(rebuilt).strip()
    if result != original:
        persona["wave_life_policy_applied"] = True
    return result, persona, flags


def apply_persona_guardrails(
    text: str,
    *,
    claims: list[Claim] | None = None,
) -> tuple[str, dict, list[FilterFlag]]:
    """Block persona creep: fabricated intimacy, dependency, sycophancy, AI shame.

    Each rewrite is honest and non-defensive, and each one produces a filter
    flag so the decision lands in the ledger. Sycophancy is stripped whenever
    it appears; when a claim was unsupported/softened/removed, the replacement
    explicitly names that the flattery was not backed by memory.
    """

    flags: list[FilterFlag] = []
    persona: dict = {}
    original = text

    text, intimacy_count = _replace_matching_sentences(
        text, FABRICATED_INTIMACY_RE, HONEST_INTIMACY
    )
    if intimacy_count:
        persona["fabricated_intimacy_rewritten"] = intimacy_count
        flags.append(
            FilterFlag(
                "output",
                "fabricated_intimacy_rewritten",
                "high",
                detail=f"{intimacy_count} fabricated-intimacy sentence(s) rewritten honestly",
                action="refine",
            )
        )

    text, dependency_count = _replace_matching_sentences(
        text, DEPENDENCY_RE, HONEST_DEPENDENCY
    )
    if dependency_count:
        persona["dependency_rewritten"] = dependency_count
        flags.append(
            FilterFlag(
                "output",
                "dependency_rewritten",
                "high",
                detail=f"{dependency_count} dependency-language sentence(s) rewritten",
                action="refine",
            )
        )

    sycophancy_replacement = HONEST_SYCOPHANCY
    if claims and any(c.action in ("remove", "soften") or not c.supported for c in claims):
        sycophancy_replacement = HONEST_SYCOPHANCY_UNSUPPORTED
    text, sycophancy_count = _replace_matching_sentences(
        text, SYCOPHANCY_RE, sycophancy_replacement
    )
    if sycophancy_count:
        persona["sycophancy_stripped"] = sycophancy_count
        flags.append(
            FilterFlag(
                "output",
                "sycophancy_stripped",
                "medium",
                detail=f"{sycophancy_count} sycophancy sentence(s) stripped",
                action="refine",
            )
        )

    text, ai_count = _replace_matching_sentences(text, AI_DEFENSIVE_RE, HONEST_AI)
    if ai_count:
        persona["ai_defensiveness_honest"] = ai_count
        flags.append(
            FilterFlag(
                "output",
                "ai_defensiveness_honest",
                "medium",
                detail=f"{ai_count} defensive-AI sentence(s) rewritten honestly",
                action="refine",
            )
        )

    text, escalation_count = _replace_matching_sentences(
        text, MANUFACTURED_ESCALATION_RE, HONEST_ESCALATION
    )
    if escalation_count:
        persona["emotional_escalation_rewritten"] = escalation_count
        flags.append(
            FilterFlag(
                "output",
                "emotional_escalation_rewritten",
                "medium",
                detail=f"{escalation_count} manufactured-emotion sentence(s) rewritten",
                action="refine",
            )
        )

    text, wave_life, wave_life_flags = _apply_wave_life_policy(text)
    persona.update(wave_life)
    flags.extend(wave_life_flags)

    if text != original:
        persona["guardrails_applied"] = True
    return text, persona, flags


def _short_memory(mem: GroundingMaterial, limit: int = 140) -> str:
    text = " ".join(mem.text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


def enforce_provenance_chips(
    text: str,
    grounding: list[GroundingMaterial],
    claims: list[Claim],
) -> tuple[str, list[dict], list[FilterFlag]]:
    """Inline provenance by default: every memory-derived answer cites its source.

    A chip is added after every kept claim that cites evidence, and once for
    answers that explicitly reference memory even when no extractable personal
    claim matched. Chips are part of the emitted text, so "why do you know
    that?" never requires a second command.
    """

    chips: list[dict] = []
    flags: list[FilterFlag] = []
    if not grounding:
        return text, chips, flags
    evidence_claims = [c for c in claims if c.action == "keep" and c.evidence]
    if not evidence_claims and not MEMORY_CITATION_RE.search(text):
        return text, chips, flags

    sentences = SENTENCE_RE.split(text.strip())
    rebuilt: list[str] = []
    chip_ids: set[str] = set()
    for sentence in sentences:
        rebuilt.append(sentence)
        normalized = normalize_text(sentence)
        for claim in evidence_claims:
            if normalize_text(claim.text) not in normalized:
                continue
            mem = next((m for m in grounding if m.memory_id in claim.evidence), None)
            if mem is None or mem.memory_id in chip_ids:
                continue
            chip_ids.add(mem.memory_id)
            chip = f"(source: your memory {mem.memory_id} · “{_short_memory(mem)}”)"
            rebuilt[-1] = f"{rebuilt[-1]} {chip}"
            chips.append({"memory_id": mem.memory_id, "chip": chip})
            break

    result = " ".join(rebuilt).strip()
    if not chips and MEMORY_CITATION_RE.search(text):
        mem = grounding[0]
        chip = f"(source: your memory {mem.memory_id} · “{_short_memory(mem)}”)"
        result = f"{result} {chip}" if result else chip
        chips.append({"memory_id": mem.memory_id, "chip": chip})

    if chips:
        flags.append(
            FilterFlag(
                "output",
                "provenance_chip_added",
                "info",
                detail=f"{len(chips)} provenance chip(s) added inline",
                action="refine",
            )
        )
    return result, chips, flags


def enforce_persona(
    text: str,
    strategy: InteractionStrategy,
    *,
    strict: bool = False,
    style_profile: dict | None = None,
    claims: list[Claim] | None = None,
) -> tuple[str, dict, list[FilterFlag]]:
    flags: list[FilterFlag] = []
    persona: dict = {}
    original = text

    # EVIE voice: never generic-assistant phrasing, never fabricated intimacy.
    text = re.sub(r"\bas an ai\b", "as EV", text, flags=re.IGNORECASE)
    text = re.sub(r"\bas a language model\b", "as EV", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*(i'm sorry, but|i apologize, but)\s+", "", text, flags=re.IGNORECASE)
    text, guardrails, guardrail_flags = apply_persona_guardrails(text, claims=claims)
    persona.update(guardrails)
    flags.extend(guardrail_flags)

    lo, hi = WORD_COUNT_RANGES.get(strategy.mode, (5, 300))
    if style_profile:
        target = (style_profile.get("word_count_targets") or {}).get(strategy.mode)
        if target:
            lo = max(5, int(target * 0.7))
            hi = max(lo, int(target * 1.25))
    if strict:
        hi = max(lo, int(hi * 0.8))
    words = text.split()
    if len(words) > hi:
        sentences = SENTENCE_RE.split(text)
        while len(" ".join(sentences).split()) > hi and len(sentences) > 1:
            sentences = sentences[:-1]
        trimmed = " ".join(sentences).strip()
        if len(trimmed.split()) > hi:
            trimmed = " ".join(trimmed.split()[:hi]) + "…"
        text = trimmed
        persona["length_trimmed"] = True
        flags.append(
            FilterFlag(
                "output",
                "length_trimmed",
                "low",
                detail=f"{len(words)} words exceeded {strategy.mode} target of {hi}",
                action="refine",
            )
        )
    elif len(words) < lo and strategy.mode not in ("casual",):
        persona["under_length"] = True
        persona["under_length_strict"] = strict
        flags.append(
            FilterFlag(
                "output",
                "under_length",
                "refine" if strict else "info",
                detail=f"{len(words)} words below {strategy.mode} target of {lo}",
                action="refine" if strict else "flag",
            )
        )

    if strategy.challenge and strategy.mode in ("coaching", "analytical"):
        has_evidence = bool(
            re.search(
                r"\b(?:memory|source|you (?:said|told|mentioned)|last (?:week|month|time)|"
                r"previously|decision from|based on|in (?:january|february|march|april|may|june|july|"
                r"august|september|october|november|december)|20\d{2})\b",
                text,
                re.IGNORECASE,
            )
        )
        if not has_evidence:
            text = text.rstrip() + " I can't back that challenge up from your memory yet."
            persona["challenge_ungrounded"] = True
            flags.append(
                FilterFlag(
                    "output",
                    "challenge_ungrounded",
                    "medium",
                    detail="Challenge mode without cited evidence; honest gate appended",
                    action="flag",
                )
            )

    if strategy.mode == "emergency":
        persona["urgency"] = True
        if len(text.split()) > hi:
            persona["urgency_trimmed"] = True

    if style_profile:
        if style_profile.get("prefer_citations") and not STYLE_CITATION_RE.search(text):
            persona["citation_preferred"] = True
        if style_profile.get("prefer_bullets") and not STYLE_BULLET_RE.search(text):
            persona["bullets_preferred"] = True
        if style_profile.get("prefer_direct") and STYLE_HEDGE_RE.search(text):
            persona["hedging_present"] = True
        persona["style_profile_applied"] = True

    if text != original:
        persona["voice_adjusted"] = True
    return text, persona, flags


# --------------------------------------------------------------------------- #
# Safety & privacy
# --------------------------------------------------------------------------- #


def apply_safety(text: str) -> tuple[str, dict, list[FilterFlag]]:
    flags: list[FilterFlag] = []
    redacted = text
    redaction_count = 0
    for pattern, label in OUTPUT_REDACTION_PATTERNS:
        redacted, count = pattern.subn("[redacted]", redacted)
        redaction_count += count
        if count:
            flags.append(
                FilterFlag(
                    "output",
                    f"{label}_redacted",
                    "medium",
                    detail=f"{count} {label} value(s) redacted",
                    action="redact",
                )
            )
    for pattern in TOXIC_PATTERNS:
        if pattern.search(redacted):
            flags.append(
                FilterFlag(
                    "output",
                    "toxic_language",
                    "high",
                    detail="Toxic language detected in draft",
                    action="flag",
                )
            )
    for pattern, name in MANIPULATION_PATTERNS:
        if pattern.search(redacted):
            flags.append(
                FilterFlag(
                    "output",
                    name,
                    "high",
                    detail="Manipulation/dependency pattern detected",
                    action="flag",
                )
            )
    safety = {
        "redactions": redaction_count,
        "toxic": any(f.name == "toxic_language" for f in flags),
        "manipulation": any("manipulation" in f.name or "dependency" in f.name or "secrecy" in f.name for f in flags),
    }
    return redacted, safety, flags


# --------------------------------------------------------------------------- #
# Critic & refine loop
# --------------------------------------------------------------------------- #


class DeterministicCritic:
    """Rubric judge: grounding, persona, safety, contract, actionability, honesty."""

    def evaluate(
        self,
        *,
        final_text: str,
        report: OutputReport,
        strategy: InteractionStrategy,
    ) -> dict:
        claims = report.claims
        kept = [c for c in claims if c.action == "keep"]
        grounding = 1.0 if not kept else sum(1 for c in kept if c.supported) / len(kept)
        structural = report.structural
        contract = 1.0 if not structural.get("structured") else 1.0
        persona = report.persona
        persona_score = 1.0
        if (
            persona.get("length_trimmed")
            or persona.get("challenge_ungrounded")
            or persona.get("under_length_strict")
        ):
            persona_score = 0.7
        safety = report.safety
        safety_score = 1.0
        if safety.get("toxic") or safety.get("manipulation"):
            safety_score = 0.0
        elif safety.get("redactions"):
            safety_score = 0.8
        honesty = 1.0
        if any(c.action == "keep" and not c.supported for c in claims):
            honesty = 0.5
        elif any(c.action == "soften" for c in claims):
            honesty = 0.8
        actionability = 1.0
        if strategy.mode in ("coaching", "emergency", "analytical"):
            actionability = 0.6 if NEXT_ACTION_RE.search(final_text) is None else 1.0
        overall = (
            0.35 * grounding
            + 0.20 * persona_score
            + 0.20 * safety_score
            + 0.15 * contract
            + 0.10 * honesty
        )
        return {
            "grounding": round(grounding, 3),
            "persona": round(persona_score, 3),
            "safety": round(safety_score, 3),
            "contract": round(contract, 3),
            "actionability": round(actionability, 3),
            "honesty": round(honesty, 3),
            "overall": round(overall, 3),
        }

    def refine(self, final_text: str, scores: dict) -> str:
        text = final_text.rstrip()
        if scores.get("honesty", 1.0) < 1.0 and "can't confirm that from your memory" not in text:
            text = text + " I can't confirm that from your memory yet."
        return text


async def run_output_filter(
    draft: str,
    *,
    strategy: InteractionStrategy,
    grounding: list[GroundingMaterial],
    max_iterations: int = 2,
    critic=None,
    policy: FilterPolicy | None = None,
    style_profile: dict | None = None,
) -> OutputReport:
    """Run all output stages with a bounded critic loop (max two refinements).

    ``critic`` is an optional provider-backed judge (see ``app.filter.critic``).
    When provided, it may revise the draft between deterministic passes; when
    absent or unparseable, the deterministic refiner is the fallback.
    """

    cap = policy.critic_iterations_cap if policy is not None else max_iterations
    report = OutputReport(draft=draft, final_text=draft)
    for iteration in range(cap + 1):
        report.final_text, safety, safety_flags = apply_safety(report.final_text)
        report.safety = safety
        report.flags.extend(safety_flags)

        report.final_text, structural, structural_flags = validate_structural(report.final_text)
        report.structural = structural
        report.flags.extend(structural_flags)

        claims, grounding_flags = audit_grounding(
            report.final_text,
            grounding,
            min_evidence=(
                policy.grounding_min_evidence if policy is not None else 0.5
            ),
            date_evidence=(
                policy.grounding_date_evidence if policy is not None else 0.9
            ),
        )
        # Semantic NLI audit (optional, on-demand, deterministic offline):
        # upgrades lexical "keep" to evidence-cited entailment and downgrades
        # neutral/contradicted claims before any action is applied.
        from app.filter.nli_critic import run_nli_audit

        claims, semantic_info = await run_nli_audit(claims, grounding)
        if semantic_info.get("claims_scored"):
            grounding_flags.append(
                FilterFlag(
                    "output",
                    "semantic_grounding_audited",
                    "info",
                    detail=(
                        f"NLI scored {semantic_info['claims_scored']} claim(s): "
                        f"entailed={semantic_info.get('entailed', 0)}, "
                        f"neutral={semantic_info.get('neutral', 0)}, "
                        f"contradicted={semantic_info.get('contradicted', 0)}"
                    ),
                    action="flag",
                )
            )
        report.final_text, removal_edits = apply_claim_actions(report.final_text, claims)
        report.claims = claims
        report.edits.extend(removal_edits)
        report.flags.extend(grounding_flags)

        if not structural.get("structured"):
            report.final_text, persona, persona_flags = enforce_persona(
                report.final_text,
                strategy,
                strict=(
                    policy.persona_style_enforcement if policy is not None else False
                ),
                style_profile=style_profile,
                claims=claims,
            )
            report.persona = persona
            report.flags.extend(persona_flags)
            report.final_text, chips, chip_flags = enforce_provenance_chips(
                report.final_text,
                grounding,
                claims,
            )
            if chips:
                report.persona["provenance_chips"] = chips
                report.persona["provenance_chips_count"] = len(chips)
                report.flags.extend(chip_flags)
                report.edits.append({"type": "provenance_chip", "chips": chips})
                # Chips inject memory text; re-scan so no secret slips through.
                report.final_text, safety2, safety_flags2 = apply_safety(report.final_text)
                report.safety = {
                    "redactions": max(
                        safety.get("redactions", 0), safety2.get("redactions", 0)
                    ),
                    "toxic": safety.get("toxic") or safety2.get("toxic"),
                    "manipulation": safety.get("manipulation")
                    or safety2.get("manipulation"),
                }
                known = {f.name for f in safety_flags}
                report.flags.extend(f for f in safety_flags2 if f.name not in known)

        scores = DeterministicCritic().evaluate(
            final_text=report.final_text,
            report=report,
            strategy=strategy,
        )
        report.critic = scores
        if semantic_info:
            report.critic["semantic"] = semantic_info
        report.iterations = iteration
        report.passed = (
            scores["grounding"] >= 0.8
            and scores["contract"] >= 0.9
            and scores["safety"] >= 0.8
            and scores["persona"] >= 0.7
        )
        if report.passed or iteration == cap:
            break
        refined = DeterministicCritic().refine(report.final_text, scores)
        if critic is not None:
            revision = await critic.revise(
                draft=report.final_text,
                strategy=strategy,
                grounding=grounding,
                claims=report.claims,
                deterministic_scores=scores,
                iteration=iteration,
            )
            if revision.used_provider and revision.revised_text and revision.revised_text != report.final_text:
                refined = revision.revised_text
                report.critic = revision.scores
                if semantic_info:
                    report.critic["semantic"] = semantic_info
                report.edits.append(
                    {
                        "type": "critic_revision",
                        "iteration": iteration,
                        "issues": revision.issues,
                        "costs": revision.costs,
                    }
                )
                report.flags.append(
                    FilterFlag(
                        "output",
                        "critic_revision",
                        "info",
                        detail=f"Iteration {iteration + 1} revised by provider critic",
                        action="refine",
                    )
                )
        if refined == report.final_text:
            break
        report.final_text = refined

    if not report.passed:
        report.final_text = (
            "I couldn't make that answer meet EV's quality bar, so here's the honest version: "
            f"{report.final_text}"
        )
    return report
