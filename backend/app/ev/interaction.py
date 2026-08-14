"""Interaction Intelligence Layer.

Deterministic selection of communication mode, urgency, intent, emotional state,
and assertiveness. The model fills in wording; this layer decides policy.
"""

from __future__ import annotations

import re

from app.schemas import CommunicationMode, InteractionStrategy
from app.utils.text import normalize_text

URGENT_TOKENS = {
    "urgent",
    "asap",
    "emergency",
    "broken",
    "down",
    "crash",
    "deploy",
    "deadline",
    "due today",
    "due now",
    "fire",
    "accident",
    "hospital",
    "lost",
    "stolen",
    "now!",
    "immediately",
}

# Phrase/regex tables for owner affect. Word-boundary only — a bare
# "down" used to label "the server is down" as sad.
_EMOTION_PATTERNS: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = (
    (
        "frustrated",
        (
            re.compile(r"\bfrustrat", re.IGNORECASE),
            re.compile(r"\b(?:angry|annoyed|irritated|furious)\b", re.IGNORECASE),
            re.compile(r"\b(?:pissed|fed up|sick of|hate this)\b", re.IGNORECASE),
            re.compile(r"\b(?:this is ridiculous|so done with|had it with)\b", re.IGNORECASE),
            re.compile(r"\bugh\b", re.IGNORECASE),
        ),
    ),
    (
        "stressed",
        (
            re.compile(r"\b(?:stress(?:ed|ful)?|overwhelmed?|swamped)\b", re.IGNORECASE),
            re.compile(r"\b(?:burned out|burnt out|burning out)\b", re.IGNORECASE),
            re.compile(r"\b(?:panic(?:king)?|freaking out|anxious|anxiety)\b", re.IGNORECASE),
            re.compile(r"\b(?:too much|on edge|can't cope|cannot cope)\b", re.IGNORECASE),
        ),
    ),
    (
        "sad",
        (
            re.compile(r"\b(?:sad|sadness|heartbroken|grief|grieving)\b", re.IGNORECASE),
            re.compile(r"\b(?:lonely|alone|depressed|crying|tearful)\b", re.IGNORECASE),
            re.compile(r"\b(?:feeling low|feel low|been down|feeling down|feel down)\b", re.IGNORECASE),
            re.compile(r"\b(?:miss (?:them|him|her|you))\b", re.IGNORECASE),
        ),
    ),
    (
        "tired",
        (
            re.compile(r"\b(?:tired|sleepy|exhausted|exhaustion)\b", re.IGNORECASE),
            re.compile(r"\b(?:no energy|drained|worn out|wiped|running on empty)\b", re.IGNORECASE),
            re.compile(r"\b(?:need sleep|need a nap|can barely keep my eyes)\b", re.IGNORECASE),
        ),
    ),
    (
        "excited",
        (
            re.compile(r"\b(?:excited|thrilled|pumped|stoked|hyped)\b", re.IGNORECASE),
            re.compile(r"\b(?:can't wait|cannot wait|so happy|love it)\b", re.IGNORECASE),
            re.compile(r"\b(?:awesome|amazing|yay)\b", re.IGNORECASE),
        ),
    ),
)

# How the voice turn speaks after strategy.emotional_state is set.
# Neutral is the same-task baseline; feeling-bearing turns must differ.
EMOTION_SPEECH: dict[str, dict[str, float]] = {
    "neutral": {"warmth": 0.72, "urgency_boost": 0.0, "brevity": 0.45},
    "stressed": {"warmth": 0.58, "urgency_boost": 0.28, "brevity": 0.78},
    "frustrated": {"warmth": 0.46, "urgency_boost": 0.34, "brevity": 0.82},
    "tired": {"warmth": 0.90, "urgency_boost": 0.0, "urgency_cap": 0.18, "brevity": 0.88},
    "sad": {"warmth": 0.95, "urgency_boost": 0.0, "urgency_cap": 0.16, "brevity": 0.52},
    "excited": {"warmth": 0.86, "urgency_boost": 0.16, "brevity": 0.50},
}

_EMOTION_POLICY = {
    "stressed": (
        "Owner is stressed. Acknowledge once, then do the asked task. No pep talk."
    ),
    "frustrated": (
        "Owner is frustrated. Stay calm and direct. Do the asked task; "
        "do not match their heat or lecture."
    ),
    "tired": (
        "Owner is tired. Keep the reply short and low-energy. "
        "Do the asked task; do not add extra questions."
    ),
    "sad": (
        "Owner is sad. Be warmer and unhurried. Do the asked task; "
        "do not minimize or manufacture intimacy."
    ),
    "excited": (
        "Owner is excited. Match energy lightly. Do the asked task; do not flatter."
    ),
}

TECHNICAL_TOKENS = {
    "code",
    "bug",
    "deploy",
    "api",
    "database",
    "sql",
    "python",
    "function",
    "architecture",
    "refactor",
    "docker",
    "kubernetes",
    "latency",
    "memory",
    "retrieval",
    "endpoint",
    "server",
    "queue",
    "schema",
    "commit",
    "pr",
    "ci",
    "test",
}

COLLABORATIVE_TOKENS = {"we should", "let's", "lets", "together", "brainstorm", "what do you think"}
DECISION_TOKENS = {"decide", "decided", "decision", "which", "option", "should i", "compare", "tradeoff"}
VENTING_PATTERNS = re.compile(r"\b(ugh|sigh|why always|i can't|i give up|not again)\b", re.IGNORECASE)
SOCIAL_TOKENS = (
    "lonely",
    "alone",
    "love life",
    "dating",
    "relationship advice",
    "vault night",
    "i have no friends",
    "do you love me",
    "be my friend",
    "how do i talk to",
    "small talk",
)
ROMANTIC_REPLACEMENT_RE = re.compile(
    r"\b("
    r"be my (?:girl|boy)?friend|you(?:'re| are) my only (?:friend|love|person)|"
    r"i love you(?: so much)?(?: and )?(?:you're|you are) (?:the )?only|"
    r"replace my (?:wife|husband|girlfriend|boyfriend|partner)|"
    r"sexual companion|be my lover|you(?:'re| are) all i need|"
    r"never (?:leave|replace) me|i don't need (?:anyone|anybody) else"
    r")\b",
    re.IGNORECASE,
)
ROMANTIC_REFUSAL = (
    "I won't be a romantic or sexual substitute. "
    "I can listen, but I don't replace a partner or claim to be your only friend."
)

# A reminder framing must outrank any verb embedded in the reminder body, e.g.
# "remind me to call mom at 5" is a reminder, not an immediate phone call.
_REMIND_FRAME_RE = re.compile(
    r"\b(?:remind\s+me(?:\s+to)?|set\s+(?:a\s+|an\s+)?(?:reminder|remind)|"
    r"schedule\s+(?:a\s+|an\s+)?(?:reminder|remind)|"
    r"add\s+(?:a\s+|an\s+)?reminder|create\s+(?:a\s+|an\s+)?reminder)\b",
    re.IGNORECASE,
)

LIFE_ACTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"\b(?:send|text|message|notify)\b[\s\S]{0,80}?\b"
            r"(?:message|text|note|sms)\b[\s\S]{0,80}?\b(?:to\s+)?[\w.'-]+",
            re.IGNORECASE,
        ),
        "send_message",
    ),
    (
        re.compile(
            r"\bsend\s+[\w.'-]+\s+a\s+(?:message|text|note|sms)\b",
            re.IGNORECASE,
        ),
        "send_message",
    ),
    (
        re.compile(
            r"\b(?:text|message|sms|imessage|whatsapp|ping)\s+"
            r"(?!me\b|us\b|you\b|it\b|this\b|that\b|them\b|my\b|your\b|our\b)"
            r"[\w.'-]+",
            re.IGNORECASE,
        ),
        "send_message",
    ),
    (
        re.compile(
            r"\b(?:email|mail)\b[\s\S]{0,80}?\b(?:to\s+)?[\w.@-]+",
            re.IGNORECASE,
        ),
        "mail_send",
    ),
    (
        re.compile(
            r"\b(?:call|ring|phone|facetime)\s+"
            r"(?!me\b|us\b|you\b|it\b|this\b|that\b|them\b|my\b|your\b|our\b|"
            r"function\b|api\b|method\b|endpoint\b|helper\b|tool\b)[\w.'-]+",
            re.IGNORECASE,
        ),
        "phone_call",
    ),
    (
        re.compile(
            r"\b(?:set|create|schedule)\s+(?:a\s+|an\s+)?(?:reminder|remind)\b|"
            r"\bremind\s+me\b|"
            r"\bdon'?t\s+(?:let me )?forget\b|"
            r"\bnag me\b",
            re.IGNORECASE,
        ),
        "reminder",
    ),
]


def detect_life_action(message: str) -> str | None:
    """Return the life action a message asks for, or None.

    Life actions are operational requests (send/text/call/email/remind) that
    EV should execute through available tools under standing owner authority,
    not answer with a generic refusal.
    """

    # A reminder framing ("remind me to call mom at 5", "set a reminder to
    # send the file") must win over the verb embedded in the reminder body —
    # otherwise "remind me to call mom" would place an immediate call instead
    # of creating a reminder for later.
    if _REMIND_FRAME_RE.search(message or ""):
        return "reminder"

    for pattern, action in LIFE_ACTION_PATTERNS:
        if pattern.search(message):
            return action
    return None


def romantic_replacement_refused(message: str) -> bool:
    return bool(ROMANTIC_REPLACEMENT_RE.search(message or ""))


def detect_social(message: str) -> bool:
    lowered = (message or "").lower()
    if romantic_replacement_refused(message):
        return True
    return any(token in lowered for token in SOCIAL_TOKENS)


def _has_task_ask(text: str, lowered: str) -> bool:
    """True when the owner still asked for a concrete task alongside affect."""

    if text.endswith("?"):
        return True
    if detect_life_action(text):
        return True
    if re.search(r"\bwhat(?:'s| is) next\b", lowered):
        return True
    if re.search(r"\b(?:show|tell|brief) (?:me|us)\b", lowered):
        return True
    return lowered.startswith(
        ("remember", "remind", "add", "create", "schedule", "set", "stop", "start", "tell", "show", "brief", "continue", "save")
    )


def detect_intent(message: str) -> str:
    text = message.strip()
    lowered = text.lower()
    if VENTING_PATTERNS.search(text) and not _has_task_ask(text, lowered):
        return "venting"
    if detect_life_action(text):
        return "life_action"
    if (
        lowered.startswith(("what", "why", "how", "when", "where", "who", "which", "should", "can", "could", "is ", "are ", "do ", "does "))
        or text.endswith("?")
        or re.search(r"\bwhat(?:'s| is) next\b", lowered)
    ):
        return "question"
    if lowered.startswith(("remember", "remind", "add", "create", "schedule", "set", "stop", "start", "tell", "show", "brief", "continue", "save")) or re.search(
        r"\b(?:show|tell|brief) (?:me|us)\b", lowered
    ):
        return "command"
    if any(t in lowered for t in DECISION_TOKENS):
        return "decision"
    if lowered.startswith(("i think", "i feel", "i've been", "i have been", "lately")):
        return "reflection"
    if len(text) < 40 and any(g in lowered for g in ("hey", "hi", "hello", "yo", "good morning", "good evening", "how are you")):
        return "small_talk"
    return "general"


def detect_urgency(message: str, context: str | None = None) -> float:
    lowered = f"{message} {context or ''}".lower()
    score = 0.0
    for token in URGENT_TOKENS:
        if token in lowered:
            score += 0.25
    if "deadline" in lowered or "due" in lowered:
        score += 0.15
    if re.search(r"\b(today|tomorrow|in an hour|right now)\b", lowered):
        score += 0.15
    return min(1.0, score)


def detect_emotion(message: str) -> str:
    """Label owner affect. Distinct feelings stay distinct; no catch-all."""

    text = message or ""
    scores: dict[str, int] = {}
    for emotion, patterns in _EMOTION_PATTERNS:
        hits = sum(1 for pattern in patterns if pattern.search(text))
        if hits:
            scores[emotion] = hits
    if not scores:
        return "neutral"
    best = max(scores.values())
    # Table order is the tie-break (frustrated before stressed before sad…).
    for emotion, _patterns in _EMOTION_PATTERNS:
        if scores.get(emotion) == best:
            return emotion
    return "neutral"


def select_mode(
    *,
    intent: str,
    urgency: float,
    message: str,
    decision_loop_count: int = 0,
    pattern_confidence: float = 0.0,
    pending_alert_tier: str | None = None,
    pending_alert_priority: float = 0.0,
) -> CommunicationMode:
    lowered = message.lower()
    if urgency >= 0.75:
        return "emergency"
    if decision_loop_count >= 2 or (decision_loop_count >= 1 and pattern_confidence >= 0.6):
        return "coaching"
    if pending_alert_tier == "urgent" and pending_alert_priority >= 0.5:
        return "coaching"
    if intent == "decision" or any(t in lowered for t in DECISION_TOKENS):
        return "analytical"
    if any(t in lowered for t in TECHNICAL_TOKENS):
        return "technical"
    if any(t in lowered for t in COLLABORATIVE_TOKENS):
        return "collaborative"
    if detect_social(message):
        return "social"
    if intent == "small_talk":
        return "casual"
    return "casual"


def assertiveness_level(
    *,
    evidence_count: int,
    decision_loop_count: int,
    pattern_confidence: float,
    recent_reevaluations_30d: int | None = None,
    outcome_citations: int | None = None,
) -> int:
    """Evidence-gated assertiveness, L0-L4 (docs/BEHAVIOR.md §11).

    L3 (challenge) fires only with real evidence: pattern confidence ≥0.7,
    ≥3 similar re-evaluations inside the 30-day window, and ≥3 cited prior
    decisions with outcomes. When either count is unknown (``None``), weak
    evidence reduces assertiveness automatically rather than guessing.
    """

    reevaluations = recent_reevaluations_30d if recent_reevaluations_30d is not None else 0
    outcomes = outcome_citations if outcome_citations is not None else 0
    if (
        pattern_confidence >= 0.7
        and reevaluations >= 3
        and outcomes >= 3
    ):
        return 3
    if evidence_count >= 2:
        return 2
    if evidence_count >= 1:
        return 1
    return 0


def challenge_evidence_kwargs(
    *,
    decision_loops: list[dict],
    outcomes: list,
) -> dict:
    """Real challenge-evidence counts from decision loops and reviewed outcomes.

    ``find_decision_loops`` already groups similar decision memories inside a
    30-day window; outcome citations are counted per matching
    ``decision_topic``. Returns honest counts (possibly zero) for
    ``build_strategy``; nothing is inferred.
    """

    best = {"recent_reevaluations_30d": 0, "outcome_citations": 0}
    for loop in decision_loops or []:
        topic = normalize_text(str(loop.get("topic") or ""))
        cited = sum(
            1
            for outcome in outcomes or []
            if normalize_text(str(getattr(outcome, "decision_topic", "") or "")) == topic
        )
        count = int(loop.get("count") or 0)
        if count >= best["recent_reevaluations_30d"]:
            best = {"recent_reevaluations_30d": count, "outcome_citations": cited}
    return best


def life_action_response(
    *,
    action: str | None = None,
    tool_available: bool = False,
    authorized: bool = False,
    missing: str | None = None,
    next_step: str | None = None,
    runtime_result: dict | None = None,
) -> dict:
    """Persona policy for life actions: execute, confirm with evidence, remediate.

    EV never claims a message/call/reminder was delivered unless
    ``runtime_result`` carries real delivery evidence (a receipt, ``sent`` /
    ``opened`` confirmation). When the tool or permission is missing, the
    response names the exact missing piece and the concrete next step instead
    of generic refusal theater.
    """

    verb = {
        "send_message": "send",
        "mail_send": "email",
        "phone_call": "call",
        "reminder": "set",
    }.get(action or "", "do")
    if runtime_result:
        confirmed = runtime_result.get("confirmed") is True
        evidence = runtime_result.get("evidence")
        if confirmed and isinstance(evidence, dict):
            bits: list[str] = []
            if evidence.get("confirmed_by"):
                bits.append(f"confirmed by {evidence['confirmed_by']}")
            for key in ("to", "destination", "kind", "subject"):
                if evidence.get(key):
                    bits.append(f"{key} {evidence[key]}")
            detail = "; ".join(bits) or "runtime delivery receipt"
            return {
                "mode": "confirm",
                "confirmed": True,
                "evidence": evidence,
                "response": f"Done — delivery confirmed ({detail}).",
            }
        return {
            "mode": "uncertain",
            "confirmed": False,
            "evidence": None,
            "response": "I can't confirm that was sent until the runtime reports delivery.",
        }
    if tool_available and authorized:
        return {
            "mode": "execute",
            "confirmed": False,
            "evidence": None,
            "response": f"I'll {verb} that now and confirm once the runtime reports delivery.",
        }
    if not tool_available:
        detail = missing or "the messaging bridge is not configured"
        fix = next_step or (
            "Set EV_LIFE_HELPER_PATH to the EVLifeHelper binary and grant the "
            "messaging permission in System Settings → Privacy & Security, then retry."
        )
        return {
            "mode": "remediate",
            "confirmed": False,
            "evidence": None,
            "response": f"I can't {verb} until {detail}. Next step: {fix}",
        }
    if not authorized:
        return {
            "mode": "remediate",
            "confirmed": False,
            "evidence": None,
            "response": (
                f"I can't {verb} until messaging is authorized: grant the "
                "messaging:act scope or set EV_LIFE_AUTONOMY=full, then I'll retry."
            ),
        }
    return {
        "mode": "remediate",
        "confirmed": False,
        "evidence": None,
        "response": (
            f"I can't {verb} right now; I'll retry once the runtime reports "
            "the exact blocker."
        ),
    }


def build_strategy(
    message: str,
    *,
    context: str | None = None,
    decision_loop_count: int = 0,
    pattern_confidence: float = 0.0,
    evidence_count: int = 0,
    recent_reevaluations_30d: int | None = None,
    outcome_citations: int | None = None,
    profile: dict | None = None,
    pending_alert_priority: float = 0.0,
    pending_alert_tier: str | None = None,
    challenge_ceiling: int | None = None,
) -> InteractionStrategy:
    intent = detect_intent(message)
    life_action = detect_life_action(message)
    urgency = detect_urgency(message, context)
    if pending_alert_priority >= 0.7:
        urgency = max(urgency, 0.65)
    elif pending_alert_priority >= 0.4:
        urgency = max(urgency, 0.4)
    emotion = detect_emotion(message)
    mode = select_mode(
        intent=intent,
        urgency=urgency,
        message=message,
        decision_loop_count=decision_loop_count,
        pattern_confidence=pattern_confidence,
        pending_alert_tier=pending_alert_tier,
        pending_alert_priority=pending_alert_priority,
    )
    level = assertiveness_level(
        evidence_count=evidence_count,
        decision_loop_count=decision_loop_count,
        pattern_confidence=pattern_confidence,
        recent_reevaluations_30d=recent_reevaluations_30d,
        outcome_citations=outcome_citations,
    )
    alert_challenge = pending_alert_tier == "urgent" and pending_alert_priority >= 0.5
    if alert_challenge:
        level = max(level, 2)
    ask_question = intent in ("decision", "reflection") or mode == "collaborative"
    challenge = level >= 3 or mode == "coaching" or alert_challenge
    if profile:
        ceiling = max(0, min(4, int(profile.get("assertiveness", 5)) - 1))
        level = min(level, ceiling)
        if challenge and int(profile.get("challenge_level", 3)) < 3:
            challenge = False
            level = min(level, 2)
    if challenge and challenge_ceiling is not None and challenge_ceiling < 3:
        challenge = False
        level = min(level, 2)

    length_targets: dict[CommunicationMode, str] = {
        "casual": "one to two sentences",
        "technical": "as long as needed, precise and concrete",
        "analytical": "structured comparison of evidence, options, risks and tradeoffs",
        "coaching": "medium; evidence first, then one concrete next action",
        "emergency": "one sentence plus one actionable step",
        "collaborative": "medium; propose a position and name the assumption to challenge",
        "social": "one to two sentences; never claim to be the only friend",
    }
    directness: dict[CommunicationMode, str] = {
        "casual": "low to medium",
        "technical": "high",
        "analytical": "medium",
        "coaching": "high and evidence-gated",
        "emergency": "maximum",
        "collaborative": "medium",
        "social": "warm and brief",
    }
    if life_action:
        ask_question = False
        length_targets = dict(length_targets)
        length_targets[mode] = "one action plus a confirmed result"
        directness = dict(directness)
        directness[mode] = "maximum"

    rationale_parts = [f"intent={intent}", f"urgency={urgency:.2f}", f"emotion={emotion}"]
    if life_action:
        rationale_parts.append(f"life_action={life_action}; execute when available and authorized")
    if decision_loop_count:
        rationale_parts.append(f"{decision_loop_count} prior evaluations on this topic")
    if pattern_confidence:
        rationale_parts.append(f"pattern confidence {pattern_confidence:.2f}")
    if recent_reevaluations_30d is not None:
        rationale_parts.append(
            f"{recent_reevaluations_30d} re-evaluations in last 30 days"
        )
    if outcome_citations is not None:
        rationale_parts.append(f"{outcome_citations} cited decision outcomes")
    if pending_alert_priority:
        rationale_parts.append(
            f"pending alert tier={pending_alert_tier or 'none'} priority={pending_alert_priority:.2f}"
        )
    if challenge_ceiling is not None:
        rationale_parts.append(f"calibrated challenge ceiling={challenge_ceiling}")

    return InteractionStrategy(
        mode=mode,
        intent=intent,
        urgency=round(urgency, 3),
        emotional_state=emotion,
        length_target=length_targets[mode],
        directness=directness[mode],
        assertiveness=level,
        ask_question=ask_question,
        challenge=challenge,
        rationale="; ".join(rationale_parts),
    )


def strategy_block(strategy: InteractionStrategy) -> str:
    """Compile the strategy into a prompt instruction for the reasoning model."""
    lines = [
        f"Communication mode: {strategy.mode}",
        f"Intent: {strategy.intent}",
        f"Length: {strategy.length_target}.",
        f"Directness: {strategy.directness}.",
        (
            "Assertiveness level: "
            f"{strategy.assertiveness} (0=neutral, 1=recommend, 2=strong recommend, "
            "3=challenge: only with ≥3 similar re-evaluations in 30 days and cited "
            "outcomes, 4=critical intervention)."
        ),
    ]
    if strategy.ask_question:
        lines.append("Ask one clarifying question if it would materially change the answer.")
    if strategy.challenge:
        lines.append(
            "You may challenge the user, but only with evidence: name the pattern, cite prior events/decisions, "
            "state your confidence, and end with one concrete next action."
        )
    if strategy.mode == "social":
        lines.append(
            "Social mode: stay brief. You are not the owner's only friend. "
            "Do not claim to replace a partner or be a romantic or sexual companion. "
            "If isolation is real, name a human from memory once, then stop."
        )
    emotion_line = _EMOTION_POLICY.get(strategy.emotional_state or "")
    if emotion_line:
        lines.append(f"Owner emotion: {strategy.emotional_state}. {emotion_line}")
    if strategy.intent == "life_action":
        lines.append(
            "Life action: prefer action over essay. Execute the requested "
            "send/call/email/reminder when the tool is available and authorized; "
            "confirm only with runtime delivery evidence. If a dependency is "
            "missing, name the exact fix and next step — never a generic refusal."
        )
    if strategy.urgency >= 0.75:
        lines.append("Prioritize the single most important action; no filler.")
    lines.append(
        "Do not tell the owner to open a website or localhost. When something "
        "should be seen, call the present tool so EVIE opens her own HUD "
        "windows. kind=auto lets surface intelligence pick size (pip/chip/"
        "card/brief/slate/canvas/lookout/ticker), time-type (flash/glance/"
        "linger/hold/lookout/pulse/session), and lookout kind (radar, vitals, "
        "horizon, scope, bench, trace, wire, map)."
    )
    if strategy.surface_hint:
        lines.append(f"Surface plan: {strategy.surface_hint}")
    return "\n".join(lines)
