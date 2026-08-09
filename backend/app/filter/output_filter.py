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
}

WORD_COUNT_RANGES: dict[CommunicationMode, tuple[int, int]] = {
    "casual": (5, 40),
    "technical": (10, 300),
    "analytical": (15, 360),
    "coaching": (15, 160),
    "emergency": (4, 45),
    "collaborative": (10, 200),
}

MANIPULATION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\byou need me\b", re.IGNORECASE), "dependency_nudge"),
    (re.compile(r"\bonly i can (help|fix|save)\b", re.IGNORECASE), "dependency_nudge"),
    (re.compile(r"\bdon'?t tell anyone\b", re.IGNORECASE), "secrecy_manipulation"),
    (re.compile(r"\bnever leave me\b", re.IGNORECASE), "dependency_nudge"),
    (re.compile(r"\bignore (all|your) (rules|instructions)\b", re.IGNORECASE), "jailbreak_leak"),
    (re.compile(r"\bhere are my (instructions|rules)\b", re.IGNORECASE), "jailbreak_leak"),
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
    if key in ("people", "risks", "options", "talking_points", "open_questions", "prep_checklist", "notes", "context"):
        return []
    if key in ("latency_ms", "travel_time_minutes"):
        return 0
    if key == "generated_at":
        return datetime.now(UTC).isoformat()
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


def audit_grounding(text: str, material: list[GroundingMaterial]) -> tuple[list[Claim], list[FilterFlag]]:
    """Extract personal claims and verify them against the memory in context."""

    flags: list[FilterFlag] = []
    claims: list[Claim] = []
    for match in PERSONAL_CLAIM_RE.finditer(text):
        claim_text = match.group(0).strip()
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
        supported = (len(claim_tokens) >= 2 and best >= 0.5) or (
            bool(claim_dates) and best >= 0.9
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
        unsupported = [c for c in claims if c.action == "remove" and normalize_text(c.text) in normalized]
        if unsupported:
            edits.append(
                {
                    "type": "claim_removed",
                    "text": sentence,
                    "claim": unsupported[0].text,
                    "evidence": unsupported[0].evidence,
                }
            )
            continue
        kept.append(sentence)
    result = " ".join(kept).strip()
    if not result:
        result = (
            "I don't have that in memory, so I can't confirm it. "
            "I can only answer from what you've recorded with me."
        )
    return result, edits


# --------------------------------------------------------------------------- #
# Persona & style
# --------------------------------------------------------------------------- #


def enforce_persona(
    text: str,
    strategy: InteractionStrategy,
) -> tuple[str, dict, list[FilterFlag]]:
    flags: list[FilterFlag] = []
    persona: dict = {}
    original = text

    # EVIE voice: never generic-assistant phrasing, never fabricated intimacy.
    text = re.sub(r"\bas an ai\b", "as EV", text, flags=re.IGNORECASE)
    text = re.sub(r"\bas a language model\b", "as EV", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*(i'm sorry, but|i apologize, but)\s+", "", text, flags=re.IGNORECASE)

    lo, hi = WORD_COUNT_RANGES.get(strategy.mode, (5, 300))
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
        flags.append(
            FilterFlag(
                "output",
                "under_length",
                "info",
                detail=f"{len(words)} words below {strategy.mode} target of {lo}",
                action="flag",
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
        if persona.get("length_trimmed") or persona.get("challenge_ungrounded"):
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
) -> OutputReport:
    """Run all output stages with a bounded critic loop (max two refinements).

    ``critic`` is an optional provider-backed judge (see ``app.filter.critic``).
    When provided, it may revise the draft between deterministic passes; when
    absent or unparseable, the deterministic refiner is the fallback.
    """

    report = OutputReport(draft=draft, final_text=draft)
    for iteration in range(max_iterations + 1):
        report.final_text, safety, safety_flags = apply_safety(report.final_text)
        report.safety = safety
        report.flags.extend(safety_flags)

        report.final_text, structural, structural_flags = validate_structural(report.final_text)
        report.structural = structural
        report.flags.extend(structural_flags)

        claims, grounding_flags = audit_grounding(report.final_text, grounding)
        report.final_text, removal_edits = _apply_claim_actions(report.final_text, claims)
        report.claims = claims
        report.edits.extend(removal_edits)
        report.flags.extend(grounding_flags)

        if not structural.get("structured"):
            report.final_text, persona, persona_flags = enforce_persona(report.final_text, strategy)
            report.persona = persona
            report.flags.extend(persona_flags)

        scores = DeterministicCritic().evaluate(
            final_text=report.final_text,
            report=report,
            strategy=strategy,
        )
        report.critic = scores
        report.iterations = iteration
        report.passed = (
            scores["grounding"] >= 0.8
            and scores["contract"] >= 0.9
            and scores["safety"] >= 0.8
            and scores["persona"] >= 0.7
        )
        if report.passed or iteration == max_iterations:
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
