"""ToneController (spec §4): ordered ceiling rules + voice/audience caps.

Every rule appends a readable, rule-prefixed reason. Reasons are data:
the thread-state owner matches the "ruleN:" prefix to maintain the
serious latch. Unknown signals always degrade toward calm.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Reuses the existing turn_shape (pure: os/re/dataclasses only; allowed by
# the boundary test's narrow app.ev exception). Pinned import: nothing else
# from app.ev is reachable from this package.
from app.ev.dynamic_personality import turn_shape as _legacy_turn_shape
from app.persona import learning
from app.persona.audience import COMPANY, ON_BEHALF, resolve_audience
from app.persona.context import PersonaContext, ToneDecision
from app.persona.reactions import banter_invited, roast_me_anyway
from app.persona.settings import profanity as profanity_default

LATCH_TURNS = 3

# turn_shape bucket -> SHAPE, before seeded variety is applied.
_SHAPE_BASE = {
    "ack": "one-liner",
    "short": "short",
    "standard": "short",
    "expanded": "full",
    "briefing": "full",
}


def decide_shape(ctx: PersonaContext) -> tuple[str, str]:
    """SHAPE hint from the existing turn_shape, with seeded variety.

    Returns (shape, reason). Deterministic for (thread_id, turn_index):
    standard grows to full 30% of the time. The short->one-liner flip was
    removed after Round 1: the model reads "one-liner" as a bare quip with
    no rescue, which lost usefulness 0-8/1-7 across families. one-liner now
    only serves true acks (presence), where one sentence is correct.
    """
    legacy = _legacy_turn_shape(
        ctx.owner_msg or "",
        intent=(ctx.intent or "general"),
        mode=(ctx.mode or "casual"),
        urgency=float(ctx.urgency or 0.0),
        emotion=(ctx.emotion or "neutral"),
        profile=None,
    )
    base = _SHAPE_BASE.get(legacy.shape, "short")
    rng = learning.seeded_rng(f"{ctx.thread_id or 'none'}:shape", ctx.turn_index)
    draw = rng.random()
    shape = base
    if base == "short" and legacy.shape == "standard" and draw < 0.30:
        shape = "full"
    return shape, f"shape: turn_shape={legacy.shape} -> {shape}"

# Emotions the existing detect_emotion emits, plus "grieving" (spec §4 rule 2
# names it; treated as sensitive whenever any detector reports it).
_SUPPORT_EMOTIONS = frozenset({"stressed", "sad", "grieving"})

_HIGH_URGENCY = 0.75  # Same threshold the existing select_mode uses.


def _failed_this_turn(receipts: tuple[Any, ...]) -> str | None:
    """Name of the first failed receipt, or None. Read-only, duck-typed."""
    for receipt in receipts or ():
        if isinstance(receipt, Mapping):
            ok = receipt.get("ok")
            if ok is False:
                return str(receipt.get("name") or receipt.get("_tool") or "tool")
        else:
            ok = getattr(receipt, "ok", None)
            if ok is False:
                return str(getattr(receipt, "name", None) or "tool")
    return None


def _slider_ceiling(slider: int | None) -> int | None:
    """Map the owner's humour slider (existing 0–5 scale) to a roast cap."""
    if slider is None:
        return None
    try:
        value = int(slider)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return 0
    if value == 1:
        return 1
    if value == 2:
        return 2
    return 3


def decide_tone(ctx: PersonaContext, weights: Any = None) -> ToneDecision:
    """Decide the roast ceiling and voice for this turn (deterministic)."""
    reasons: list[str] = []
    audience = resolve_audience(ctx)
    owner_msg = ctx.owner_msg or ""
    override = roast_me_anyway(owner_msg)
    invited = banter_invited(owner_msg)

    # Rule 1: confirmation, destructive, failed receipt. No override.
    failed = _failed_this_turn(ctx.receipts)
    if ctx.confirmation_pending:
        reasons.append("rule1: confirmation pending — ROAST=0, humour off, no override")
        return _finish(ctx, audience, 0, False, reasons, weights)
    if failed is not None:
        reasons.append(f"rule1: failed receipt ({failed}) — ROAST=0, no override")
        return _finish(ctx, audience, 0, False, reasons, weights)

    # Rule 2: stressed/sad/grieving/sensitive.
    emotion = (ctx.emotion or "").strip().lower() or None
    sensitive = bool((ctx.sensitive_topic or "").strip())
    if (emotion in _SUPPORT_EMOTIONS) or sensitive:
        label = (ctx.sensitive_topic or "").strip() if sensitive else emotion
        if override:
            reasons.append(
                f"rule2: {label} but owner said roast-me-anyway — ceiling 2, one turn only"
            )
            return _finish(ctx, audience, 2, True, reasons, weights, cap=2)
        reasons.append(f"rule2: {label} — support first, ROAST=0")
        return _finish(ctx, audience, 0, False, reasons, weights)

    # Rule 3: serious latch.
    latch = max(0, int(ctx.serious_latch_turns_left or 0))
    if latch > 0:
        if override:
            reasons.append("rule3: latch_cleared by roast-me-anyway")
        elif invited:
            reasons.append("rule3: latch_cleared, owner lightened up")
        else:
            reasons.append(f"rule3: serious latch ({latch} turns left) — ROAST=0")
            return _finish(ctx, audience, 0, False, reasons, weights)

    # Rule 4: frustrated — humour aims at the object, never at him.
    target: str | None = None
    if emotion == "frustrated":
        target = "object"
        reasons.append("rule4: frustrated — humour aims at the object, never at him")

    # Rule 5: high urgency.
    urgency_cap: int | None = None
    urgency = ctx.urgency
    if isinstance(urgency, (int, float)) and urgency >= _HIGH_URGENCY:
        urgency_cap = 1
        reasons.append("rule5: high urgency — ceiling 1, brevity forced")

    # Rule 6: base + bonuses + caps.
    roast = 1
    reasons.append("rule6: base 1")
    if invited:
        roast += 1
        reasons.append("rule6: owner joked first (+1)")
    if emotion == "excited":
        roast += 1
        reasons.append("rule6: owner excited (+1)")
    if override:
        roast = 3
        reasons.append("rule6: explicit roast request — 3 allowed")
    else:
        roast = min(roast, 2)

    # Ordered caps: earlier rules, audience, channel, slider.
    if urgency_cap is not None:
        roast = min(roast, urgency_cap)
    if audience == COMPANY:
        roast = min(roast, 1)
        reasons.append("cap: company mode caps roast at 1")
    if audience == ON_BEHALF:
        roast = 0
        reasons.append("cap: on_behalf carries no Evie voice")
    if ctx.channel in ("voice", "phone"):
        roast = min(roast, 2)
        reasons.append(f"cap: {ctx.channel} caps roast at 2")
    slider_cap = _slider_ceiling(ctx.humor_slider)
    if slider_cap is not None:
        roast = min(roast, slider_cap)
        reasons.append(f"cap: humour slider ceiling {slider_cap}")

    reasons.append("rule7: density decides via the learned joke-rate draw")
    humour = roast >= 1 and audience != ON_BEHALF
    return _finish(ctx, audience, roast, humour, reasons, weights, target=target)


def _finish(
    ctx: PersonaContext,
    audience: str,
    roast: int,
    humour: bool,
    reasons: list[str],
    weights: Any,
    cap: int | None = None,
    target: str | None = None,
) -> ToneDecision:
    if cap is not None:
        roast = min(roast, cap)
    if audience == COMPANY:
        roast = min(roast, 1)
    if audience == ON_BEHALF:
        roast, humour = 0, False
    if ctx.channel in ("voice", "phone"):
        roast = min(roast, 2)
    if audience == COMPANY and "cap: company mode caps roast at 1" not in reasons:
        reasons.append("cap: company mode caps roast at 1")
    voice = "owner" if audience == ON_BEHALF else "evie"
    profanity = bool(profanity_default and humour and audience == "private")
    shape, shape_reason = decide_shape(ctx)
    reasons.append(shape_reason)
    partial = ToneDecision(
        roast=roast,
        humour_allowed=humour,
        technique=None,
        profanity=profanity,
        audience=audience,
        reasons=tuple(reasons),
        target=target,
        voice=voice,
        shape=shape,
    )
    technique = learning.hint_for(
        partial,
        weights if weights is not None else learning.LearningWeights.default(),
        learning.seeded_rng(ctx.thread_id, ctx.turn_index),
    )
    if technique is not None:
        reasons.append(f"technique: {technique} (Thompson sample)")
    else:
        reasons.append("technique: none (humour off or joke-rate draw skipped)")
    return ToneDecision(
        roast=partial.roast,
        humour_allowed=partial.humour_allowed,
        technique=technique,
        profanity=partial.profanity,
        audience=partial.audience,
        reasons=tuple(reasons),
        target=partial.target,
        voice=partial.voice,
        shape=partial.shape,
    )


def update_latch(prev: int, decision: ToneDecision) -> int:
    """Next serious-latch value for the thread-state owner.

    Clears on "latch_cleared" reasons, otherwise decays by one. Setting the
    latch to LATCH_TURNS is the caller's job when a rule2/rule3 decision
    lands (match the "rule2:"/"rule3:" reason prefixes).
    """
    if any("latch_cleared" in reason for reason in decision.reasons):
        return 0
    return max(0, int(prev or 0) - 1)


def joke_flag(reply: str | None, decision: ToneDecision) -> bool:
    """Whether this turn counts as a joke told (for recent_joke_flags).

    Approximation by design: humour was permitted and a reply went out.
    The thread-state owner records the last three of these.
    """
    return bool(decision.humour_allowed and (reply or "").strip())
