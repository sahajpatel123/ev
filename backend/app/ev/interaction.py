"""Interaction Intelligence Layer.

Deterministic selection of communication mode, urgency, intent, emotional state,
and assertiveness. The model fills in wording; this layer decides policy.
"""

from __future__ import annotations

import re

from app.schemas import CommunicationMode, InteractionStrategy

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

EMOTION_KEYWORDS = {
    "stressed": ("stress", "overwhelm", "burned out", "burnt out", "panic", "too much"),
    "frustrated": ("frustrat", "angry", "annoyed", "pissed", "sick of", "hate this", "ugh"),
    "tired": ("tired", "sleepy", "exhausted", "no energy", "drained"),
    "excited": ("excited", "awesome", "amazing", "love it", "can't wait", "thrilled"),
    "sad": ("sad", "down", "lonely", "alone", "depressed", "crying"),
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


def detect_intent(message: str) -> str:
    text = message.strip()
    lowered = text.lower()
    if VENTING_PATTERNS.search(text):
        return "venting"
    if lowered.startswith(("what", "why", "how", "when", "where", "who", "which", "should", "can", "could", "is ", "are ", "do ", "does ")) or text.endswith("?"):
        return "question"
    if lowered.startswith(("remember", "remind", "add", "create", "schedule", "set", "stop", "start", "tell", "show", "brief", "continue", "save")):
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
    lowered = message.lower()
    for emotion, keywords in EMOTION_KEYWORDS.items():
        if any(k in lowered for k in keywords):
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
    if intent == "small_talk":
        return "casual"
    return "casual"


def assertiveness_level(
    *,
    evidence_count: int,
    decision_loop_count: int,
    pattern_confidence: float,
) -> int:
    if pattern_confidence >= 0.7 and decision_loop_count >= 2:
        return 3
    if evidence_count >= 2:
        return 2
    if evidence_count >= 1:
        return 1
    return 0


def build_strategy(
    message: str,
    *,
    context: str | None = None,
    decision_loop_count: int = 0,
    pattern_confidence: float = 0.0,
    evidence_count: int = 0,
    profile: dict | None = None,
    pending_alert_priority: float = 0.0,
    pending_alert_tier: str | None = None,
) -> InteractionStrategy:
    intent = detect_intent(message)
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

    length_targets: dict[CommunicationMode, str] = {
        "casual": "one to two sentences",
        "technical": "as long as needed, precise and concrete",
        "analytical": "structured comparison of evidence, options, risks and tradeoffs",
        "coaching": "medium; evidence first, then one concrete next action",
        "emergency": "one sentence plus one actionable step",
        "collaborative": "medium; propose a position and name the assumption to challenge",
    }
    directness: dict[CommunicationMode, str] = {
        "casual": "low to medium",
        "technical": "high",
        "analytical": "medium",
        "coaching": "high and evidence-gated",
        "emergency": "maximum",
        "collaborative": "medium",
    }

    rationale_parts = [f"intent={intent}", f"urgency={urgency:.2f}", f"emotion={emotion}"]
    if decision_loop_count:
        rationale_parts.append(f"{decision_loop_count} prior evaluations on this topic")
    if pattern_confidence:
        rationale_parts.append(f"pattern confidence {pattern_confidence:.2f}")
    if pending_alert_priority:
        rationale_parts.append(
            f"pending alert tier={pending_alert_tier or 'none'} priority={pending_alert_priority:.2f}"
        )

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
        f"Assertiveness level: {strategy.assertiveness} (0=neutral, 1=recommend, 2=strong recommend, 3=challenge, 4=critical intervention).",
    ]
    if strategy.ask_question:
        lines.append("Ask one clarifying question if it would materially change the answer.")
    if strategy.challenge:
        lines.append(
            "You may challenge the user, but only with evidence: name the pattern, cite prior events/decisions, "
            "state your confidence, and end with one concrete next action."
        )
    if strategy.urgency >= 0.75:
        lines.append("Prioritize the single most important action; no filler.")
    return "\n".join(lines)
