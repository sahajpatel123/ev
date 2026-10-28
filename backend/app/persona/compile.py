"""compile.py — THE single entry point: build_persona_block(ctx).

v2.1 assembly: CORE (+ TEXT ADDENDUM on text) + NOW line + TURN TONE
(with SHAPE) + opportunistic CALLBACK / LEDGER lines + up to 2 exemplars.

Drop order when over budget: exemplars, then the addendum, then NOW-line
detail (full -> short -> clock-only -> omitted). CORE lines are never
dropped. The `dropped` tuple records exactly what was shed.
"""

from __future__ import annotations

import re
from typing import Any

from app.persona import tokens
from app.persona.awareness import build_now_line, signals_from_context
from app.persona.context import PersonaBlock, PersonaContext, ToneDecision
from app.persona.core import addendum_only, core_only
from app.persona.exemplars import MAX_EXEMPLARS
from app.persona.exemplars import select as select_exemplars
from app.persona.learning import LearningWeights
from app.persona.opinions import Stance
from app.persona.reactions import topic_of
from app.persona.settings import PERSONA_VERSION
from app.persona.tone import decide_tone

TEXT_BUDGET = 750
VOICE_BUDGET = 450

_BUDGETS = {"text": TEXT_BUDGET, "voice": VOICE_BUDGET, "phone": VOICE_BUDGET}

# Owner message takes a stance on the turn's topic (ledger trigger).
_STANCE_TAKING_RE = re.compile(
    r"\bi (think|believe|'ve decided|'m going with)|"
    r"\bgoing with\b|\bwe should\b|\blet'?s\b|\bmy take\b|\bdecided\b",
    re.IGNORECASE,
)


def budget_for(channel: str) -> int:
    """Token budget for a channel. Unknown channels get the voice budget."""
    return _BUDGETS.get((channel or "").strip().lower(), VOICE_BUDGET)


def render_tone_line(decision: ToneDecision) -> str:
    """The fixed TURN TONE line, with suffixes only when active."""
    line = (
        f"TURN TONE: ROAST={decision.roast}; AUDIENCE={decision.audience}; "
        f"PROFANITY={'on' if decision.profanity else 'off'}; "
        f"TECHNIQUE={decision.technique or 'none'}; SHAPE={decision.shape}"
    )
    if decision.roast == 0:
        # Round 1: two ROAST=0 turns still opened with a joke. Spell out
        # the ban where the model cannot miss it.
        line += "; HUMOUR=OFF: plain, no jokes or witty openers"
    if decision.target == "object" and decision.humour_allowed:
        # A humour target with humour off is a contradiction; drop it.
        line += "; TARGET=object"
    if decision.voice == "owner":
        line += "; VOICE=owner"
    return line


def _callback_line(store: Any, ctx: PersonaContext, decision: ToneDecision) -> str:
    """A landed-bit callback hint, or "" when none applies."""
    suggest = getattr(store, "suggest_callback", None)
    if store is None or suggest is None:
        return ""
    if decision.technique != "callback":
        return ""
    key = ctx.thread_id or "owner:default"
    try:
        bit = suggest(key)
    except Exception:
        return ""
    if not bit:
        return ""
    bit = " ".join(str(bit).split())
    if len(bit) > 120:
        bit = bit[:119].rstrip() + "…"
    return f"CALLBACK (landed, use at most once this conversation): {bit}"


def _ledger_line(ledger: Any, ctx: PersonaContext) -> str:
    """A one-line own-the-change reminder, or "" when none applies."""
    if ledger is None or not (ctx.owner_msg or "").strip():
        return ""
    if not _STANCE_TAKING_RE.search(ctx.owner_msg or ""):
        return ""
    topic = topic_of(ctx.owner_msg)
    try:
        priors = ledger.recent(topic, 3)
    except Exception:
        return ""
    if not priors:
        return ""
    try:
        clashes = ledger.contradicts(
            Stance(topic=topic, text=(ctx.owner_msg or "")[:160], reason="", ts="now")
        )
    except Exception:
        return ""
    if not clashes:
        return ""
    prior = priors[-1]
    reason = f" because {prior.reason}" if prior.reason else ""
    return (
        f"LEDGER: you said {prior.text[:120]!r} ({prior.ts}){reason} — "
        "own it or own the change."
    )


def _shorten_now(signals: dict, stage: int) -> str:
    """NOW line at detail stage 0 (full) .. 3 (omitted)."""
    if stage <= 0:
        return build_now_line(signals)
    if stage == 1:
        return build_now_line(signals, detail="short")
    if stage == 2:
        clock = signals.get("local_time")
        tiny = build_now_line({"local_time": clock} if clock is not None else {})
        return tiny
    return ""


def build_persona_block(
    ctx: PersonaContext,
    store: Any = None,
    weights: LearningWeights | None = None,
    ledger: Any = None,
    *,
    include_core: bool = True,
    budget: int | None = None,
) -> PersonaBlock:
    """Compile the persona block for one turn (deterministic, <5 ms).

    `include_core=False` (plus a reduced `budget`) serves the delegation
    path in dynamic_personality, where the caller appends the core itself.
    """
    if weights is None:
        weights = store.load_weights() if store is not None else LearningWeights.default()
    decision = decide_tone(ctx, weights)
    if budget is None:
        budget = budget_for(ctx.channel)

    is_text = (ctx.channel or "").strip().lower() == "text"
    core = core_only() if include_core else ""
    addendum = addendum_only() if (is_text and include_core) else ""
    # Delegation path still carries the addendum (text callers only).
    if is_text and not include_core:
        addendum = addendum_only()
    signals = signals_from_context(ctx)
    tone_line = render_tone_line(decision)
    callback = _callback_line(store, ctx, decision)
    ledger_line = _ledger_line(ledger, ctx)
    eligible = select_exemplars(ctx, decision, 10**9)[:MAX_EXEMPLARS]

    dropped: list[str] = []
    picked: list = list(eligible)
    now_stage = 0

    def assemble(extra_lines: list[str]) -> tuple[str, int]:
        parts = []
        if core:
            parts.append(core.rstrip())
        if addendum:
            parts.append(addendum.rstrip())
        now = _shorten_now(signals, now_stage)
        if now:
            parts.append(now)
        parts.append(tone_line)
        parts.extend(extra_lines)
        for item in picked:
            parts.append(item.rendered())
        text = "\n\n".join(parts) + "\n"
        return text, tokens.estimate(text)

    # Shed loop in drop order: exemplars, then addendum, then NOW detail.
    text, estimate = assemble([])
    for _ in range(12):
        if estimate <= budget:
            break
        if picked:
            removed = picked.pop()
            dropped.append(f"exemplar:{removed.id}")
        elif addendum:
            addendum = ""
            dropped.append("addendum")
        elif now_stage < 3:
            now_stage += 1
            dropped.append("now_detail")
        else:
            break
        text, estimate = assemble([])

    # Opportunistic fill: ledger + callback lines only when they fit.
    extras: list[str] = []
    for candidate in (ledger_line, callback):
        if not candidate:
            continue
        trial, trial_est = assemble(extras + [candidate])
        if trial_est <= budget:
            extras.append(candidate)
            text, estimate = trial, trial_est

    return PersonaBlock(
        text=text,
        version=PERSONA_VERSION,
        tone=decision,
        token_estimate=estimate,
        dropped=tuple(dropped),
    )
