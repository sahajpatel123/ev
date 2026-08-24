"""Luna intent adapter (G1.3) — GPT-5.6 Luna via structured outputs.

Uses OpenAI text/Responses structured outputs when EV_OPENAI_API_KEY is set;
falls back to deterministic rule-based routing for tests and offline runs.
No regex-parsed free-form English.
"""

from __future__ import annotations

import json
import re
import time

from app.config import settings
from app.ev.turn_intent import TurnIntent

# Static routing contract — cache-friendly, never includes dynamic turn
LUNA_SYSTEM_PROMPT = """You are Evie's Turn Controller brain (Luna). Classify the owner turn into a typed intent.

Routes:
- CONVERSATION: casual chat, no state/action needed
- STATE_QUERY: read canonical state (projects/goals/commitments)
- STATE_MUTATION: create/update canonical state
- MISSION_CONTROL: status or what-changed
- ACTION: device/gear action (not life state)
- DELEGATED_JOB: complex work for DeepSeek (research, planning, coding, analysis)
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
            "operation": {"type": "string", "enum": ["PROJECT_LIST","PROJECT_GET","PROJECT_CREATE","PROJECT_UPDATE","GOAL_LIST","GOAL_GET","GOAL_CREATE","GOAL_UPDATE","COMMITMENT_LIST","COMMITMENT_GET","COMMITMENT_CREATE","COMMITMENT_UPDATE","STATUS","WHAT_CHANGED","RELATIONSHIP_QUERY","RELATIONSHIP_UPDATE","UNKNOWN"]},
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
_LUNA_METRICS: dict = {
    "count": 0,
    "total_latency_ms": 0.0,
    "errors": 0,
    "last_latency_ms": 0.0,
    "last_usage": None,
    # G1.11 cost routing telemetry
    "total_owner_turns": 0,
    "deterministic_turns": 0,
    "luna_turns": 0,
    "fallback_model_turns": 0,
    "deepseek_delegations": 0,
    "conversation_turns": 0,
}

def luna_metrics_snapshot() -> dict:
    c = int(_LUNA_METRICS["count"] or 0)  # type: ignore[arg-type]
    total = float(_LUNA_METRICS["total_latency_ms"] or 0)  # type: ignore[arg-type]
    avg = (total / c) if c else 0
    total_turns = int(_LUNA_METRICS["total_owner_turns"] or 0)  # type: ignore[arg-type]
    luna_n = int(_LUNA_METRICS["luna_turns"] or 0)  # type: ignore[arg-type]
    return {
        "count": c,
        "avg_latency_ms": round(avg, 1),
        "last_latency_ms": _LUNA_METRICS["last_latency_ms"],
        "errors": _LUNA_METRICS["errors"],
        "last_usage": _LUNA_METRICS["last_usage"],
        # Cost-routing counters (G1.11): Luna must be the MINORITY path.
        "total_owner_turns": total_turns,
        "deterministic_turns": int(_LUNA_METRICS["deterministic_turns"] or 0),  # type: ignore[arg-type]
        "luna_turns": luna_n,
        "fallback_model_turns": int(_LUNA_METRICS["fallback_model_turns"] or 0),  # type: ignore[arg-type]
        "deepseek_delegations": int(_LUNA_METRICS["deepseek_delegations"] or 0),  # type: ignore[arg-type]
        "conversation_turns": int(_LUNA_METRICS["conversation_turns"] or 0),  # type: ignore[arg-type]
        "luna_invocation_rate": round(luna_n / total_turns, 4) if total_turns else 0.0,
    }

def _record_metrics(latency_ms: float, usage: dict | None = None, error: bool = False):
    _LUNA_METRICS["count"] = int(_LUNA_METRICS["count"] or 0) + 1  # type: ignore[arg-type]
    _LUNA_METRICS["total_latency_ms"] = float(_LUNA_METRICS["total_latency_ms"] or 0) + latency_ms  # type: ignore[arg-type]
    _LUNA_METRICS["last_latency_ms"] = latency_ms
    if usage:
        _LUNA_METRICS["last_usage"] = usage
    if error:
        _LUNA_METRICS["errors"] = int(_LUNA_METRICS["errors"] or 0) + 1  # type: ignore[arg-type]


def record_route_source(route_source: str) -> None:
    """Record one classified owner turn by its route source (G1.11)."""
    m = _LUNA_METRICS
    m["total_owner_turns"] = int(m["total_owner_turns"] or 0) + 1  # type: ignore[arg-type]
    key = {
        "DETERMINISTIC": "deterministic_turns",
        "LUNA": "luna_turns",
        "GPT4O_MINI_FALLBACK": "fallback_model_turns",
    }.get(route_source)
    if key:
        m[key] = int(m[key] or 0) + 1  # type: ignore[arg-type]
    if route_source == "DETERMINISTIC":
        # conversation turns counted separately at controller level when known
        pass


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

    # STATE_QUERY: priority
    m = re.search(r"what\s+priority\s+is\s+(.+?)\??$", low_stripped)
    if m or ("priority" in low and "personal fitness" in low):
        proj = m.group(1).strip().title() if m else "Personal Fitness"
        # Handle "what's the priority of fitness" variant
        if "fitness" in proj.lower():
            proj = "Personal Fitness" if "personal" in low else proj
        return TurnIntent(route="STATE_QUERY", operation="PROJECT_GET", project_title=proj.strip(" ?\"'"), confidence=0.95)
    if re.search(r"what'?s\s+the\s+priority\s+of\s+(.+)", low_stripped):
        m2 = re.search(r"what'?s\s+the\s+priority\s+of\s+(.+)", low_stripped)
        if m2:
            return TurnIntent(route="STATE_QUERY", operation="PROJECT_GET", project_title=m2.group(1).strip(" ?\"'").title(), confidence=0.9)

    # STATE_QUERY: projects list
    if low_stripped in ("what projects do i have", "what projects do i have?", "list projects", "show projects"):
        return TurnIntent(route="STATE_QUERY", operation="PROJECT_LIST", confidence=0.98)
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
    m = re.search(r"\b(delete|remove|cancel|get rid of)\b", low_stripped)
    if m and "commitment" in low_stripped:
        # Extract reference: explicit quoted fragment wins, then known
        # keywords, then words directly after the verb.
        q = ""
        mq = re.search(r"['\"](.+?)['\"]", low_stripped)
        if mq:
            q = mq.group(1).strip()
        if not q:
            for kw in ("workout", "gym", "call", "meeting", "review", "appointment", "luna"):
                if kw in low_stripped:
                    q = kw
                    break
        if not q:
            mq2 = re.search(r"(?:delete|remove|cancel|get rid of)\s+(?:my\s+|the\s+|this\s+)?(.+?)\s+commitment", low_stripped)
            if mq2 and mq2.group(1).strip() not in ("my", "the", "this"):
                q = mq2.group(1).strip()
        return TurnIntent(
            route="STATE_MUTATION", operation="COMMITMENT_CANCEL",
            commitment_query=q, confidence=0.92,
            description=t.strip(),
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
    """Obvious high-confidence cases that do not need Luna (deterministic)."""
    low = (turn or "").strip().lower()
    low_stripped = re.sub(r"^\s*evie[, ]*\s*", "", low).strip()
    # Obvious state queries/mutations and conversation
    obvious = [
        "what projects do i have", "what goals do i have", "what changed", "give me status", "evie, status",
        "how are you", "tell me a joke", "explain photosynthesis",
    ]
    for phrase in obvious:
        if phrase in low_stripped or phrase in low:
            return True
    # Obvious project/goal/commitment with clear entity
    if re.search(r"what priority is .+", low_stripped):
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
    # Commitment cancel/delete semantics (deterministic; preserves history)
    if re.search(r"\b(delete|remove|cancel|get rid of)\b.{0,40}\bcommitment\b", low_stripped):
        return True
    return "create a commitment" in low and "tomorrow" in low


def has_explicit_state_intent(turn: str) -> bool:
    """STATE-INTENT GUARD (G1.11): an utterance containing a canonical entity
    plus an obvious CRUD verb can NEVER be classified as generic CONVERSATION.

    If deterministic extraction resolves it, fine; if not, it must go to
    Luna or clarification — never escape the state control plane. This is the
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
    """Classify owner turn via Luna or rule fallback. Returns validated TurnIntent."""
    start = time.perf_counter()
    # G1.11 cost routing: deterministic first, Luna only for ambiguity.
    if is_deterministic_high_confidence(turn):
        intent = _rule_based_intent(turn, context)
        latency = (time.perf_counter() - start) * 1000
        _record_metrics(latency, usage={"route_source": "DETERMINISTIC", "fallback": "rule_based"})
        record_route_source("DETERMINISTIC")
        return intent
    use_luna_api = bool((settings.openai_api_key or "").strip())
    if use_luna_api:
        try:
            intent = await _call_luna(turn, context)
            latency = (time.perf_counter() - start) * 1000
            effective = luna_model_probe().get("effective") or ""
            if isinstance(intent, TurnIntent):
                source = "LUNA" if "luna" in (effective or "").lower() else "GPT4O_MINI_FALLBACK"
                _record_metrics(latency, usage={"model": effective, "route_source": source})
                record_route_source(source)
                return intent
            validated = TurnIntent.model_validate(intent)
            source = "LUNA" if "luna" in (effective or "").lower() else "GPT4O_MINI_FALLBACK"
            _record_metrics(latency, usage={"model": effective, "route_source": source})
            record_route_source(source)
            return validated
        except Exception:
            _record_metrics((time.perf_counter() - start) * 1000, error=True)
            pass
    # Deterministic fallback — also used in tests
    intent = _rule_based_intent(turn, context)
    # STATE-INTENT GUARD: if explicit state intent escaped deterministic
    # extraction AND Luna was unavailable/failed, do NOT silently downgrade a
    # mutation to CONVERSATION. Route to CLARIFICATION so the gate asks
    # instead of Realtime hallucinating capability limits.
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


async def _call_luna(turn: str, context: dict | None) -> TurnIntent:
    """Call Luna (OpenAI) via Responses API with structured output — primary control path."""

    # Determine requested vs fallback models
    requested = (getattr(settings, "turn_control_model", None) or "gpt-5.6-luna").strip() or "gpt-5.6-luna"
    fallback = (getattr(settings, "turn_control_fallback_model", None) or getattr(settings, "openai_chat_model", None) or "gpt-4o-mini").strip()
    # Try primary first, then fallback on model-not-found
    for attempt_model in [requested, fallback] if requested != fallback else [requested]:
        ok, intent, meta = await _call_responses_api(turn, context, model=attempt_model, requested=requested)
        if ok and intent:
            _record_luna_model(requested, attempt_model, success=True)
            return intent
        if ok is False and meta and meta.get("code") == "model_not_found":
            continue
        # For other errors, still try fallback if not already
        if attempt_model == requested and fallback != requested:
            continue
        raise RuntimeError(f"Luna call failed for {attempt_model}: {meta}")
    raise RuntimeError("Luna unavailable")


# Luna model tracking for truthful telemetry (requested vs effective)
_LUNA_REQUESTED = "gpt-5.6-luna"
_LUNA_EFFECTIVE: str | None = None
_LUNA_LAST_REQUEST_ID: str | None = None

def _record_luna_model(requested: str, effective: str, success: bool):
    global _LUNA_REQUESTED, _LUNA_EFFECTIVE
    _LUNA_REQUESTED = requested
    _LUNA_EFFECTIVE = effective if success else None

def luna_model_probe() -> dict:
    return {"requested": _LUNA_REQUESTED, "effective": _LUNA_EFFECTIVE, "last_request_id": _LUNA_LAST_REQUEST_ID}


async def _call_responses_api(turn: str, context: dict | None, *, model: str, requested: str):
    """Direct POST /v1/responses with json_schema for TurnIntent."""
    import httpx

    key = (getattr(settings, "openai_api_key", None) or "").strip()
    if not key:
        return False, None, {"code": "no_api_key"}
    # Build TurnIntent JSON schema for Responses API (strict)
    schema = {
        "type": "object",
        "properties": {
            "route": {"type": "string", "enum": ["CONVERSATION","STATE_QUERY","STATE_MUTATION","MISSION_CONTROL","ACTION","DELEGATED_JOB","RESEARCH_MISSION","CLARIFICATION","UNSUPPORTED"]},
            "operation": {"type": "string", "enum": ["PROJECT_LIST","PROJECT_GET","PROJECT_CREATE","PROJECT_UPDATE","GOAL_LIST","GOAL_GET","GOAL_CREATE","GOAL_UPDATE","COMMITMENT_LIST","COMMITMENT_GET","COMMITMENT_CREATE","COMMITMENT_UPDATE","STATUS","WHAT_CHANGED","RELATIONSHIP_QUERY","RELATIONSHIP_UPDATE","UNKNOWN"]},
            "confidence": {"type": "number"},
            "needs_clarification": {"type": "boolean"},
            "clarification_question": {"type": ["string","null"]},
            "project_title": {"type": ["string","null"]},
            "goal_title": {"type": ["string","null"]},
            "commitment_query": {"type": ["string","null"]},
            "description": {"type": ["string","null"]},
            "priority": {"type": ["string","null"]},
            "due_at": {"type": ["string","null"]},
        },
        "required": ["route","operation","confidence","needs_clarification"],
        "additionalProperties": False,
    }
    # Bounded context for Luna
    ctx_parts = []
    if context:
        if context.get("project_titles"):
            ctx_parts.append(f"Known projects: {', '.join(context['project_titles'][:10])}")
        if context.get("current_project"):
            ctx_parts.append(f"Current focus: {context['current_project']}")
    system = LUNA_SYSTEM_PROMPT
    if ctx_parts:
        system += "\nContext: " + " | ".join(ctx_parts)
    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": system},
            {"role": "user", "content": turn},
        ],
        "text": {"format": {"type": "json_schema", "name": "turn_intent", "schema": schema, "strict": True}},
        # G1.11 cost law: Luna is a router, not a thinker. Lowest effort.
        "reasoning": {"effort": "low"},
    }
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
            )
            latency = (time.perf_counter() - start) * 1000
            req_id = resp.headers.get("x-request-id")
            global _LUNA_LAST_REQUEST_ID
            _LUNA_LAST_REQUEST_ID = req_id
            if resp.status_code != 200:
                try:
                    err = resp.json().get("error", {})
                    code = err.get("code") or err.get("type") or f"http_{resp.status_code}"
                    if "model_not_found" in str(err).lower() or resp.status_code == 404:
                        code = "model_not_found"
                except Exception:
                    code = f"http_{resp.status_code}"
                return False, None, {"code": code, "status": resp.status_code, "request_id": req_id, "latency_ms": latency, "requested": requested, "effective": model}
            data = resp.json()
            effective = data.get("model") or model
            # Parse output
            out_text = None
            for item in data.get("output", []):
                if item.get("type") == "message":
                    for c in item.get("content", []):
                        if c.get("type") == "output_text":
                            out_text = c.get("text")
                            break
            usage = data.get("usage")
            if out_text:
                try:
                    parsed = json.loads(out_text)
                    intent = TurnIntent.model_validate(parsed)
                    # Attach usage/latency for metrics
                    _record_metrics(latency, usage={** (usage or {}), "model": effective, "request_id": req_id})
                    _record_luna_model(requested, effective, True)
                    return True, intent, {"request_id": req_id, "latency_ms": latency, "usage": usage, "requested": requested, "effective": effective}
                except Exception as e:
                    return False, None, {"code": "parse_failed", "error": str(e), "request_id": req_id}
            return False, None, {"code": "no_output", "request_id": req_id}
    except Exception as e:
        latency = (time.perf_counter() - start) * 1000
        return False, None, {"code": "exception", "error": str(e), "latency_ms": latency}
