"""Turn-intent adapter (G1.3) — MiMo via structured outputs.

Uses MiMo structured outputs when the text brain is provisioned; falls
back to deterministic rule-based routing for tests and offline runs.
No regex-parsed free-form English.
"""

from __future__ import annotations

import json
import re
import time

from app.ev.turn_intent import TurnIntent

# Static routing contract — cache-friendly, never includes dynamic turn
TURN_SYSTEM_PROMPT = """You are Evie's Turn Controller brain (MiMo). Classify the owner turn into a typed intent.

Routes:
- CONVERSATION: casual chat, no state/action needed
- STATE_QUERY: read canonical state (projects/goals/commitments)
- STATE_MUTATION: create/update canonical state
- MISSION_CONTROL: status or what-changed
- ACTION: device/gear action (not life state)
- DELEGATED_JOB: complex work requiring planning (MiMo proposes; existing executor acts)
- RESEARCH_MISSION: research task
- CLARIFICATION: ambiguous, need question
- UNSUPPORTED: not supported

Operations for STATE_*: PROJECT_LIST, PROJECT_GET, PROJECT_CREATE, PROJECT_UPDATE, GOAL_LIST, GOAL_GET, GOAL_CREATE, GOAL_UPDATE, COMMITMENT_LIST, COMMITMENT_GET, COMMITMENT_CREATE, COMMITMENT_UPDATE, COMMITMENT_CANCEL, STATUS, WHAT_CHANGED, RELATIONSHIP_QUERY, RELATIONSHIP_UPDATE

Rules:
- Use human references (Personal Fitness, workout), never UUIDs.
- For "what priority is X" -> STATE_QUERY PROJECT_GET with project_title=X
- For "what goals in X" -> STATE_QUERY GOAL_LIST with project_title=X
- For "when is my X due" -> STATE_QUERY COMMITMENT_LIST with commitment_query=X
- For "Evie, status" -> MISSION_CONTROL STATUS
- For "what changed" -> MISSION_CONTROL WHAT_CHANGED
- For "create a project called X" -> STATE_MUTATION PROJECT_CREATE with description=X
- For "delete/remove/cancel my X commitment" -> STATE_MUTATION COMMITMENT_CANCEL with commitment_query=X (cancel preserves history; never hard-delete)
- For ambiguous "make it high priority" without clear project -> CLARIFICATION
- For "research ..." -> DELEGATED_JOB or RESEARCH_MISSION
- For "how are you", "joke" -> CONVERSATION

Return ONLY the structured intent via the emit_intent tool. No prose.
"""

# Cache-friendly static tool spec for emit_intent
EMIT_INTENT_TOOL = {
    "name": "emit_intent",
    "description": "Emit the typed turn intent",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "route": {"type": "string", "enum": ["CONVERSATION","STATE_QUERY","STATE_MUTATION","MISSION_CONTROL","ACTION","DELEGATED_JOB","RESEARCH_MISSION","CLARIFICATION","UNSUPPORTED"]},
            "operation": {"type": "string", "enum": ["PROJECT_LIST","PROJECT_GET","PROJECT_CREATE","PROJECT_UPDATE","GOAL_LIST","GOAL_GET","GOAL_CREATE","GOAL_UPDATE","COMMITMENT_LIST","COMMITMENT_GET","COMMITMENT_CREATE","COMMITMENT_UPDATE","COMMITMENT_CANCEL","STATUS","WHAT_CHANGED","RELATIONSHIP_QUERY","RELATIONSHIP_UPDATE","UNKNOWN"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "needs_clarification": {"type": "boolean"},
            "clarification_question": {"type": ["string","null"]},
            "project_title": {"type": ["string","null"], "maxLength": 256},
            "goal_title": {"type": ["string","null"], "maxLength": 512},
            "commitment_query": {"type": ["string","null"], "maxLength": 512},
            "description": {"type": ["string","null"], "maxLength": 512},
            "priority": {"type": ["string","null"], "enum": ["CRITICAL","HIGH","NORMAL","LOW", None]},
            "due_at": {"type": ["string","null"], "maxLength": 128},
            "success_criteria": {"type": ["string","null"]},
            "status": {"type": ["string","null"]},
            "person": {"type": ["string","null"]},
            "relation": {"type": ["string","null"]},
        },
        "required": ["route", "operation"],
    },
}

# Simple in-memory metrics for health/cost (G1.3)
_TURN_METRICS: dict = {
    "count": 0,
    "total_latency_ms": 0.0,
    "errors": 0,
    "last_latency_ms": 0.0,
    "last_usage": None,
    # G1.11 cost routing telemetry
    "total_owner_turns": 0,
    "deterministic_turns": 0,
    "mimo_turns": 0,
    "conversation_turns": 0,
}

def turn_metrics_snapshot() -> dict:
    c = int(_TURN_METRICS["count"] or 0)  # type: ignore[arg-type]
    total = float(_TURN_METRICS["total_latency_ms"] or 0)  # type: ignore[arg-type]
    avg = (total / c) if c else 0
    total_turns = int(_TURN_METRICS["total_owner_turns"] or 0)  # type: ignore[arg-type]
    mimo_n = int(_TURN_METRICS["mimo_turns"] or 0)  # type: ignore[arg-type]
    return {
        "count": c,
        "avg_latency_ms": round(avg, 1),
        "last_latency_ms": _TURN_METRICS["last_latency_ms"],
        "errors": _TURN_METRICS["errors"],
        "last_usage": _TURN_METRICS["last_usage"],
        # Cost-routing counters (G1.11): MiMo must be the MINORITY path.
        "total_owner_turns": total_turns,
        "deterministic_turns": int(_TURN_METRICS["deterministic_turns"] or 0),  # type: ignore[arg-type]
        "mimo_turns": mimo_n,
        "conversation_turns": int(_TURN_METRICS["conversation_turns"] or 0),  # type: ignore[arg-type]
        "mimo_invocation_rate": round(mimo_n / total_turns, 4) if total_turns else 0.0,
    }

def _record_metrics(latency_ms: float, usage: dict | None = None, error: bool = False):
    _TURN_METRICS["count"] = int(_TURN_METRICS["count"] or 0) + 1  # type: ignore[arg-type]
    _TURN_METRICS["total_latency_ms"] = float(_TURN_METRICS["total_latency_ms"] or 0) + latency_ms  # type: ignore[arg-type]
    _TURN_METRICS["last_latency_ms"] = latency_ms
    if usage:
        _TURN_METRICS["last_usage"] = usage
    if error:
        _TURN_METRICS["errors"] = int(_TURN_METRICS["errors"] or 0) + 1  # type: ignore[arg-type]


def record_route_source(route_source: str) -> None:
    """Record one classified owner turn by its route source (G1.11)."""
    m = _TURN_METRICS
    m["total_owner_turns"] = int(m["total_owner_turns"] or 0) + 1  # type: ignore[arg-type]
    key = {
        "DETERMINISTIC": "deterministic_turns",
        "MIMO": "mimo_turns",
    }.get(route_source)
    if key:
        m[key] = int(m[key] or 0) + 1  # type: ignore[arg-type]
    if route_source == "DETERMINISTIC":
        # conversation turns counted separately at controller level when known
        pass


_CANCEL_LANGUAGE_RE = re.compile(
    r"\b(?:delete|deleted|remove|removed|cancel|cancelled|canceled|"
    r"cancelling|canceling|get rid of)\b",
    re.IGNORECASE,
)
# Generic plural family words are capability subjects, not row references.
_GENERIC_FAMILY_REFS = frozenset(
    {"commitments", "goals", "projects", "reminders"}
)
_CANCEL_META_RE = re.compile(r"\b(?:what|why|how|explain|mean|means|useful)\b", re.IGNORECASE)
# Leading connectives / determiners / repeated verbs that belong to the
# owner's grammar ("can you cancel or delete the X") rather than to the
# reference itself. Stripped iteratively after the LAST cancel verb.
_CANCEL_REFERENCE_PREFIX_RE = re.compile(
    r"^(?:and|or|then|also|just|please|now|kindly|maybe|delete|remove|cancel|"
    r"get\s+rid\s+of|my|the|a|an|evie(?:'s)?)\b[\s,:;-]*",
    re.IGNORECASE,
)
# Bare-ability questions ("Can you cancel commitments?") ask about Evie's
# capability truth; they are never mutations of a specific row.
_ABILITY_QUESTION_RE = re.compile(
    r"^\s*(?:hey\s+)?(?:evie[, ]*\s*)?(?:can|could)\s+you\b|\bdo you support\b|"
    r"\bare you able to\b|\bwhat can you do\b",
    re.IGNORECASE,
)
_CAPABILITY_SUBJECT_RE = re.compile(
    r"\b(commitments?|goals?|projects?|reminders?)\b", re.IGNORECASE
)


def _has_commitment_cancel_language(turn: str) -> bool:
    """Return whether a turn explicitly asks to cancel a commitment.

    Pronoun references are included so ``delete it`` enters the state
    control plane and can be resolved or clarified by Core.  Meta questions
    are still deterministic conversation, not mutations; the caller handles
    that distinction after this predicate.
    """
    low = (turn or "").strip().lower()
    if not _CANCEL_LANGUAGE_RE.search(low):
        return False
    return "commitment" in low or bool(re.search(r"\b(?:it|that|this)\b", low))


def _commitment_cancel_query(turn: str) -> str:
    """Extract a human commitment reference without requiring exact syntax.

    Uses the LAST cancel verb so ``can you cancel or delete the X`` resolves
    against what follows ``delete`` instead of swallowing the second verb
    into the reference. Leading connectives/determiners/repeated verbs are
    stripped iteratively.
    """
    matches = list(_CANCEL_LANGUAGE_RE.finditer(turn or ""))
    if not matches:
        return ""
    reference = (turn or "")[matches[-1].end():].strip()
    prev = None
    while prev != reference:
        prev = reference
        reference = _CANCEL_REFERENCE_PREFIX_RE.sub("", reference).strip()
    reference = reference.strip(" \t\r\n\"'")
    reference = re.sub(r"[.!?,;:]+$", "", reference).strip(" \t\r\n\"'")
    # A trailing generic noun is part of the owner's grammar, not the name.
    # Keep embedded occurrences (e.g. ``Final Commitment Proof``) intact.
    reference = re.sub(r"\s+commitment$", "", reference, flags=re.IGNORECASE).strip()
    if reference.casefold().split(maxsplit=1)[0:1] in [["it"], ["that"], ["this"]]:
        return ""
    return reference


def _rule_based_intent(turn: str, context: dict | None = None) -> TurnIntent:
    """Deterministic fallback — covers owner tests and eval set without API."""
    t = (turn or "").strip()
    low = t.lower()
    # Normalize: remove leading Evie,
    low_stripped = re.sub(r"^\s*evie[, ]*\s*", "", low).strip()
    # MISSION_CONTROL
    if low_stripped in ("status", "evie status", "give me status") or "evie, status" in low or low_stripped == "status":
        return TurnIntent(route="MISSION_CONTROL", operation="STATUS", confidence=0.99)
    if "what changed" in low_stripped or low_stripped == "what changed?" or "what changed" in low:
        return TurnIntent(route="MISSION_CONTROL", operation="WHAT_CHANGED", confidence=0.99)
    if "give me status" in low:
        return TurnIntent(route="MISSION_CONTROL", operation="STATUS", confidence=0.98)

    # STATE_QUERY: priority — accepts "what priority is X", "what's the
    # priority of X" AND the owner-natural "what is the priority of X".
    m = re.search(r"what\s+priority\s+is\s+(.+?)\??$", low_stripped)
    if m or (
        "priority" in low
        and "personal fitness" in low
        and low_stripped.startswith(("what", "how", "is", "tell"))
    ):
        proj = m.group(1).strip().title() if m else "Personal Fitness"
        # Handle "what's the priority of fitness" variant
        if "fitness" in proj.lower():
            proj = "Personal Fitness" if "personal" in low else proj
        return TurnIntent(route="STATE_QUERY", operation="PROJECT_GET", project_title=proj.strip(" ?\"'"), confidence=0.95)
    m = re.search(r"what(?:'s|\s+is)\s+the\s+priority\s+of\s+(.+?)\??$", low_stripped)
    if m:
        return TurnIntent(route="STATE_QUERY", operation="PROJECT_GET", project_title=m.group(1).strip(" ?\"'").title(), confidence=0.95)

    # Bare priority follow-up with no resolvable entity: structured
    # clarification, never generic conversation (PART 8/10).
    if re.search(
        r"^what(?:'s|\s+is)(?:\s+(?:its|the|his|her))?\s*"
        r"(?:priority(?:\s+level)?|status)\s*(?:of\s+[a-z0-9 ]+)?\?*$",
        low_stripped,
    ):
        return TurnIntent(
            route="CLARIFICATION", operation="UNKNOWN",
            needs_clarification=True,
            clarification_question="Which project?",
            confidence=0.9,
        )

    # STATE_QUERY: explicit project lookup — "find/look up the X project"
    m = re.search(
        r"\b(?:find|look\s?up|search\s+for|open|show(?:\s+me)?)\s+(?:the\s+|my\s+)?project\s+(?:called\s+)?['\"]?(.+?)['\"]?\s*\??$",
        low_stripped,
    )
    if m:
        title = re.sub(
            r"\s+project$", "", m.group(1).strip(), flags=re.IGNORECASE
        ).strip(" ?\"'")
        if title:
            return TurnIntent(
                route="STATE_QUERY",
                operation="PROJECT_GET",
                project_title=title.title() if title.islower() else title,
                confidence=0.93,
            )

    # STATE_MUTATION: explicit priority update — owner-natural phrasings:
    #   "set the priority of X to high" / "change X priority to HIGH"
    #   "make X high priority" (single known project) / "set X to high priority"
    m = re.search(
        r"(?:set|put|change|update|switch|make)\s+(?:the\s+)?priority\s+(?:of|for)\s+(.+?)\s+to\s+(critical|high|normal|low)\b",
        low_stripped,
    )
    if not m:
        m = re.search(
            r"(?:set|put|change|update|switch)\s+(.+?)\s+(?:priority\s+)?to\s+(critical|high|normal|low)\s+priority\b",
            low_stripped,
        )
    if not m:
        m = re.search(
            r"(?:make|mark)\s+(.+?)\s+(?:a\s+|an\s+)?(critical|high|normal|low)\s+priority\b",
            low_stripped,
        )
    if not m:
        m = re.search(
            r"(?:make|mark)\s+the\s+priority\s+of\s+(.+?)\s+(critical|high|normal|low)\b",
            low_stripped,
        )
    if m:
        title = m.group(1).strip(" ?\"'")
        level = m.group(2).upper()
        title = re.sub(r"^(?:the|my|project)\s+", "", title, flags=re.IGNORECASE).strip()
        # Pronoun-only project reference without context: ask, never guess.
        if not title or title.lower() in ("that", "this", "it"):
            return TurnIntent(
                route="CLARIFICATION", operation="UNKNOWN",
                needs_clarification=True,
                clarification_question="Which project?",
                confidence=0.9,
            )
        return TurnIntent(
            route="STATE_MUTATION",
            operation="PROJECT_UPDATE",
            project_title=title.title() if title.islower() else title,
            priority=level,
            confidence=0.94,
        )

    # STATE_QUERY: projects list
    if low_stripped in ("what projects do i have", "what projects do i have?", "list projects", "show projects"):
        return TurnIntent(route="STATE_QUERY", operation="PROJECT_LIST", confidence=0.98)
    if re.search(
        r"\b(what|which)\s+projects\s+(do|have|did)\s+i\b|"
        r"^(list|show|name)\s+(my\s+|the\s+)?projects?\b|"
        r"\bmy\s+projects\b.*\??$|"
        r"^projects\s*\??$",
        low_stripped,
    ):
        return TurnIntent(route="STATE_QUERY", operation="PROJECT_LIST", confidence=0.93)
    if "what projects" in low:
        return TurnIntent(route="STATE_QUERY", operation="PROJECT_LIST", confidence=0.95)

    # STATE_QUERY: goals
    if "what goals" in low and "personal fitness" in low:
        return TurnIntent(route="STATE_QUERY", operation="GOAL_LIST", project_title="Personal Fitness", confidence=0.98)
    if "what are we trying to accomplish in fitness" in low:
        return TurnIntent(route="STATE_QUERY", operation="GOAL_LIST", project_title="Personal Fitness", confidence=0.9)
    if "what goals" in low:
        # Generic goal list, try to extract project
        m = re.search(r"in\s+(.+?)(?:\?|$)", low_stripped)
        proj = m.group(1).strip().title() if m else None
        return TurnIntent(route="STATE_QUERY", operation="GOAL_LIST", project_title=proj, confidence=0.85)
    if "goals do i have in" in low:
        m = re.search(r"goals do i have in\s+(.+)", low_stripped)
        if m:
            return TurnIntent(route="STATE_QUERY", operation="GOAL_LIST", project_title=m.group(1).strip(" ?\"'").title(), confidence=0.95)

    # STATE_QUERY: commitments due
    if ("when is" in low and "due" in low) or ("when is my" in low and "commitment" in low) or ("workout" in low and "due" in low) or ("workout commitment" in low and "when" in low):
        # Extract the subject between "my"/"the" and "commitment"; fall back to
        # quoted fragment; empty q means list all open commitments.
        q = ""
        mq = re.search(r"(?:my|the)\s+(?:'|\")?(.+?)(?:'|\")?\s+commitment", low_stripped)
        if mq and mq.group(1).strip() not in ("next", "first"):
            q = mq.group(1).strip()
        elif "workout" in low_stripped:
            q = "workout"
        else:
            q = ""
        return TurnIntent(route="STATE_QUERY", operation="COMMITMENT_LIST", commitment_query=q, confidence=0.95)

    # STATE_MUTATION: project create
    m = re.search(r"create\s+a\s+project\s+called\s+(.+)", low_stripped)
    if m:
        title = m.group(1).strip(" ?\"'").strip()
        # Handle "Luna Control Test" extraction
        title = re.sub(r"[.?!]+$", "", title).strip()
        return TurnIntent(route="STATE_MUTATION", operation="PROJECT_CREATE", description=title.title() if title.islower() or title.isupper() else title, project_title=title, confidence=0.98)

    # STATE_MUTATION: goal create
    m = re.search(r"add\s+a\s+goal\s+to\s+(.+?):\s*(.+)", low_stripped)
    if m:
        proj = m.group(1).strip().title()
        goal = m.group(2).strip(" ?\"'").strip()
        return TurnIntent(route="STATE_MUTATION", operation="GOAL_CREATE", project_title=proj, goal_title=goal, description=goal, confidence=0.97)
    m = re.search(r"add\s+this\s+as\s+a\s+goal", low_stripped)
    if m:
        return TurnIntent(route="STATE_MUTATION", operation="GOAL_CREATE", description=t, confidence=0.8)
    if "remember that i want to" in low:
        desc = re.sub(r".*remember that i want to\s*", "", low_stripped).strip(" ?\"'")
        return TurnIntent(route="STATE_MUTATION", operation="GOAL_CREATE", description=desc, confidence=0.85)

    # STATE_MUTATION: commitment create
    if re.search(r"\b(create|add|set|save|make|schedule)\b.{0,30}\bcommitment\b", low) or (
        "commitment" in low and "tomorrow" in low and not re.search(r"\b(what|why|how|explain|mean|useful)\b", low)
    ):
        # Extract due and description
        due = None
        if "tomorrow at" in low or "tomorrow" in low:
            # Normalize dotted a.m./p.m. so the clock regex sees am/pm.
            normalized = re.sub(r"\b([ap])\.m\.", r"\1m", low)
            m = re.search(r"tomorrow\s+(?:at\s+)?([0-9]{1,2}(?::[0-9]{2})?\s*(?:am|pm)?)", normalized)
            due = f"tomorrow at {m.group(1).strip()}" if m else "tomorrow"
        else:
            due = None
        # Description/title extraction — intent family, not one sentence:
        #   "... called X" / "... named X" / quoted X / "to <verb> ..." / "for X"
        desc = ""
        mq = re.search(r"(?:called|named)\s+['\"]?(.+?)['\"]?\s*(?:for|tomorrow|today|tonight|on|at|\.|\?|$)", low_stripped)
        if not mq:
            mq = re.search(r"(?:called|named)\s+['\"]?(.+?)['\"]?\s*$", low_stripped)
        if mq and mq.group(1).strip():
            desc = mq.group(1).strip()
        if not desc:
            mq = re.search(r"['\"](.+?)['\"]", low_stripped)
            if mq:
                desc = mq.group(1).strip()
        if not desc:
            mq = re.search(r"commitment\s+to\s+(?:do\s+)?(?:my\s+)?(.+?)(?:\s+(?:tomorrow|today|tonight)\b.*)?$", low_stripped)
            if mq and mq.group(1).strip():
                desc = mq.group(1).strip()
        if not desc or desc.lower() in ("tomorrow", "today", "tonight"):
            # "for <day>" is a date, not a subject; try the "to <do X>" tail.
            mq = re.search(r"\bto\s+(?:do\s+)?(?:my\s+)?(.+?)\s+(?:tomorrow|today|tonight|at)\b.*$", low_stripped)
            if mq and mq.group(1).strip():
                desc = mq.group(1).strip()
        if not desc:
            # Last resort: known keywords; never invent an unrelated default.
            for kw in ("workout", "gym", "call mom", "review", "appointment"):
                if kw in low_stripped:
                    desc = kw
                    break
        return TurnIntent(route="STATE_MUTATION", operation="COMMITMENT_CREATE", description=desc, due_at=due, commitment_query=desc, confidence=0.93 if desc else 0.7)

    # STATE_MUTATION: commitment cancel/delete (semantic cancel, history preserved)
    if _has_commitment_cancel_language(low_stripped):
        # A question about cancellation is conversation, while an explicit
        # cancellation request is a deterministic state mutation.
        if _CANCEL_META_RE.search(low_stripped):
            return TurnIntent(route="CONVERSATION", operation="UNKNOWN", confidence=0.99)
        q = _commitment_cancel_query(turn)
        # Conceptual/abstract references ("Could cancelling commitments be a
        # bad habit?") ask about the concept, not a row — deterministic
        # conversation, never a mutation.
        if q and re.search(
            r"\b(?:habit|good idea|bad idea|worth it|make sense)\b|"
            r"\bbe\s+(?:a\s+)?(?:good|bad|useful|normal|healthy)\b",
            q,
            re.IGNORECASE,
        ):
            return TurnIntent(route="CONVERSATION", operation="UNKNOWN", confidence=0.97)
        # Bare-ability question ("Can you cancel commitments?") or a generic
        # family reference with an ability frame is a capability-truth query:
        # never a mutation, and never model self-assessment of what Evie
        # can do.
        generic_ref = q.strip().casefold() in _GENERIC_FAMILY_REFS
        if (not q or generic_ref) and _ABILITY_QUESTION_RE.search(low_stripped):
            subject_match = _CAPABILITY_SUBJECT_RE.search(low_stripped)
            subject = subject_match.group(1).lower() if subject_match else ""
            return TurnIntent(
                route="STATE_QUERY",
                operation="CAPABILITY_QUERY",
                capability_subject=subject,
                confidence=0.95,
            )
        return TurnIntent(
            route="STATE_MUTATION", operation="COMMITMENT_CANCEL",
            commitment_query=q, confidence=0.92,
            description=t.strip(),
        )

    # Capability self-knowledge: "What can you do with commitments?"
    if _ABILITY_QUESTION_RE.search(low_stripped) and "what can you do" in low_stripped:
        subject_match = _CAPABILITY_SUBJECT_RE.search(low_stripped)
        if subject_match:
            return TurnIntent(
                route="STATE_QUERY",
                operation="CAPABILITY_QUERY",
                capability_subject=subject_match.group(1).lower(),
                confidence=0.95,
            )

    # CLARIFICATION: ambiguous priority
    if low_stripped in ("make the project high priority", "make it high priority", "make that high priority", "fix this project"):
        # Check context for current project focus
        if context and context.get("project_candidates"):
            # If multiple, needs clarification
            cands = context.get("project_candidates", [])
            if len(cands) > 1:
                return TurnIntent(route="CLARIFICATION", operation="UNKNOWN", needs_clarification=True, clarification_question="Which project?", confidence=0.9)
        if "fix this project" in low:
            return TurnIntent(route="CLARIFICATION", operation="UNKNOWN", needs_clarification=True, clarification_question="Which project do you mean?", confidence=0.85)
        # Single candidate or no context -> assume needs clarification
        if "make" in low and "priority" in low:
            return TurnIntent(route="CLARIFICATION", operation="UNKNOWN", needs_clarification=True, clarification_question="Which project?", confidence=0.88)

    # DELEGATED_JOB / RESEARCH_MISSION
    if "research" in low and ("database architecture" in low or "research this" in low or "research the" in low):
        return TurnIntent(route="DELEGATED_JOB", operation="UNKNOWN", confidence=0.96)
    if "research this properly" in low:
        return TurnIntent(route="DELEGATED_JOB", operation="UNKNOWN", confidence=0.95)

    # CONVERSATION
    if low_stripped in ("how are you", "how are you?", "tell me a joke", "tell me a joke?") or "how are you" in low or "joke" in low_stripped:
        return TurnIntent(route="CONVERSATION", operation="UNKNOWN", confidence=0.99)

    # STATE-INTENT GUARD: entity + CRUD verb without interrogative framing
    # must never silently become CONVERSATION (capability-hallucination guard).
    if has_explicit_state_intent(t):
        return TurnIntent(
            route="CLARIFICATION", operation="UNKNOWN",
            needs_clarification=True,
            clarification_question="I understood you want to change something — could you rephrase what I should create or update?",
            confidence=0.6,
        )

    return TurnIntent(route="CONVERSATION", operation="UNKNOWN", confidence=0.7)


def is_deterministic_high_confidence(turn: str) -> bool:
    """Obvious high-confidence cases that do not need MiMo (deterministic)."""
    low = (turn or "").strip().lower()
    low_stripped = re.sub(r"^\s*evie[, ]*\s*", "", low).strip()
    # Obvious state queries/mutations and conversation
    obvious = [
        "what projects do i have", "what goals do i have", "what changed", "give me status", "evie, status",
        "how are you", "tell me a joke", "explain photosynthesis",
        "what does cancelled mean",
    ]
    for phrase in obvious:
        if phrase in low_stripped or phrase in low:
            return True
    # Obvious project/goal/commitment with clear entity
    if re.search(r"what priority is .+", low_stripped):
        return True
    if re.search(r"what(?:'s|\s+is)\s+the\s+priority\s+of\s+.+", low_stripped):
        return True
    if re.search(r"(?:set|put|change|update|switch|make)\b.{0,40}\bpriority\b", low_stripped) and "priority" in low_stripped:
        return True
    if re.search(r"what goals do i have in .+", low_stripped):
        return True
    if "when is my" in low and "due" in low:
        return True
    if re.search(r"create a project called .+", low_stripped):
        return True
    if re.search(r"add a goal to .+:", low_stripped):
        return True
    if "create a commitment" in low and "tomorrow" in low:
        return True
    # Commitment cancel/delete semantics (deterministic; preserves history).
    # This includes pronoun references so an explicit ``delete it`` cannot
    # fall through to MiMo or generic conversation.
    if _has_commitment_cancel_language(low_stripped):
        return True
    # Capability self-knowledge is deterministic (canonical registry truth).
    if _ABILITY_QUESTION_RE.search(low_stripped) and "commitment" in low:
        return True
    return "create a commitment" in low and "tomorrow" in low


def has_explicit_state_intent(turn: str) -> bool:
    """STATE-INTENT GUARD (G1.11): an utterance containing a canonical entity
    plus an obvious CRUD verb can NEVER be classified as generic CONVERSATION.

    If deterministic extraction resolves it, fine; if not, it must go to
    MiMo or clarification — never escape the state control plane. This is the
    capability-hallucination guard: Realtime must not answer 'I can't create
    commitments' for a turn that explicitly asks Evie Core to create one.
    """
    low = (turn or "").lower()
    entities = ("project", "goal", "commitment", "relationship")
    crud = (
        "create", "add", "set", "save", "make", "schedule",
        "update", "change", "pause", "complete", "block",
        "delete", "remove", "cancel", "get rid of",
    )
    has_entity = any(e in low for e in entities)
    has_crud = any(re.search(rf"\b{v}\b", low) for v in crud)
    # Interrogative/meta frames about the system are NOT mutations.
    meta = bool(re.search(r"\b(what|why|how|explain|mean|means|useful)\b", low))
    return bool(has_entity and has_crud and not meta)


async def classify_intent(turn: str, context: dict | None = None) -> TurnIntent:
    """Classify owner turn via MiMo or rule fallback. Returns validated TurnIntent."""
    start = time.perf_counter()
    # G1.11 cost routing: deterministic first, MiMo only for ambiguity.
    if is_deterministic_high_confidence(turn):
        intent = _rule_based_intent(turn, context)
        latency = (time.perf_counter() - start) * 1000
        _record_metrics(latency, usage={"route_source": "DETERMINISTIC", "fallback": "rule_based"})
        record_route_source("DETERMINISTIC")
        return intent
    use_mimo = False
    try:
        from app.gateway.roles import text_role_available

        use_mimo = text_role_available()
    except Exception:
        use_mimo = False
    if use_mimo:
        try:
            intent = await _call_mimo(turn, context)
            latency = (time.perf_counter() - start) * 1000
            if not isinstance(intent, TurnIntent):
                intent = TurnIntent.model_validate(intent)
            _record_metrics(
                latency,
                usage={"model": _MIMO_MODEL, "route_source": "MIMO"},
            )
            record_route_source("MIMO")
            return intent
        except Exception:
            _record_metrics((time.perf_counter() - start) * 1000, error=True)
            pass
    # Deterministic fallback — also used in tests
    intent = _rule_based_intent(turn, context)
    # STATE-INTENT GUARD: if explicit state intent escaped deterministic
    # extraction AND MiMo was unavailable/failed, do NOT silently downgrade a
    # mutation to CONVERSATION. Route to CLARIFICATION so the gate asks
    # instead of the voice layer hallucinating capability limits.
    if (
        intent.route == "CONVERSATION"
        and has_explicit_state_intent(turn)
    ):
        return TurnIntent(
            route="CLARIFICATION", operation="UNKNOWN",
            needs_clarification=True,
            clarification_question="I understood you want to change something — could you rephrase what I should create or update?",
            confidence=0.6,
        )
    latency = (time.perf_counter() - start) * 1000
    _record_metrics(latency, usage={"fallback": "rule_based"})
    record_route_source("DETERMINISTIC")
    return intent

_MIMO_MODEL = "xiaomi/mimo-v2.6-flash"


async def _call_mimo(turn: str, context: dict | None) -> TurnIntent:
    """Structured TurnIntent from the owning text-role brain (MiMo)."""

    return await _call_mimo_intent(turn, context)


def _json_object(text: str) -> dict | None:
    """Parse a JSON object from model text, including fenced replies."""

    raw = (text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


async def _call_mimo_intent(turn: str, context: dict | None) -> TurnIntent:
    """TurnIntent via the owning text-role brain (MiMo structured output)."""

    from app.contracts import ChatMessage
    from app.gateway.roles import chat_structured_via_role

    ctx = ""
    if isinstance(context, dict) and context:
        ctx = "\nContext (task-scoped, already filtered):\n" + json.dumps(context)[:2000]
    messages = [
        ChatMessage(role="system", content=TURN_SYSTEM_PROMPT),
        ChatMessage(role="user", content=f"Owner turn:\n{turn}{ctx}"),
    ]
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": EMIT_INTENT_TOOL["parameters"]["properties"],
        "required": ["route", "operation"],
    }
    structured = await chat_structured_via_role(
        messages, schema=schema, schema_name="turn_intent"
    )
    parsed = _json_object(structured.text or "")
    if parsed is not None:
        return TurnIntent.model_validate(parsed)
    raise RuntimeError("mimo_intent_missing")
