"""Delegation seam: dynamic_personality's per-turn functions call into here.

Under PERSONA_ENABLED (and the existing EV_PERSONALITY_DYNAMIC rollback),
turn_guidance/opine/smalltalk/mood delegate to app/persona instead of the
v4 lines. Signatures on the dynamic side are unchanged; this module takes
plain values (no foreign objects) and returns prompt strings.

Keying: call sites pass no thread id, so live state keys to
"owner:default" (documented in live.key_for). The previous Evie reply is
not visible here either, so note_turn runs with prev_reply=None: reactions
still classify against the last joke meta and latch/weights still update
and persist; landed-bit and stance capture wait for the reply hook
(personality/INTEGRATION_GAPS.md).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from app.persona.compile import TEXT_BUDGET, build_persona_block
from app.persona.context import PersonaContext, WorkState
from app.persona.core import core_only
from app.persona.live import DEFAULT_KEY, after_decision, begin_turn, key_for, note_turn
from app.persona.store import PersonaStore, default_db_path

_store: PersonaStore | None = None
_store_path: str | None = None


def live_store() -> PersonaStore:
    """Process-wide persona store (lazy; never touched when flag is off).

    Honors $EV_PERSONA_STORE (tests point it at tmp; lab at its own file).
    Rebinds when the env value changes so tests stay isolated.
    """
    global _store, _store_path
    import os

    override = os.environ.get("EV_PERSONA_STORE")
    want = str(Path(override).expanduser()) if override else str(default_db_path())
    if _store is None or _store_path != want:
        if _store is not None:
            _store.close()
        _store = PersonaStore(want, key=DEFAULT_KEY)
        _store_path = want
    return _store


def delegation_budget() -> int:
    """Budget for the delegated slice: text budget minus the core text."""
    from app.persona.tokens import estimate

    return TEXT_BUDGET - estimate(core_only())


def delegated_turn_block(
    message: str,
    *,
    intent: str = "general",
    mode: str = "casual",
    urgency: float = 0.0,
    emotion: str = "neutral",
    profile: dict | None = None,
    local_time: datetime | None = None,
    thread_id: str | None = None,
    store: Any = None,
    receipts: tuple = (),
) -> str:
    """Full v2.1 per-turn slice: addendum + NOW + TURN TONE + exemplars.

    The CORE is not included: call sites append SPEECH_STYLE_INSTRUCTIONS
    (the v2.1 core under the flag) after this, preserving contract-last.
    """
    active = store if store is not None else live_store()
    key = key_for(thread_id) if thread_id else DEFAULT_KEY
    state = begin_turn(active, key)
    humor = None
    if isinstance(profile, dict) and profile.get("humor") is not None:
        try:
            humor = int(profile["humor"])
        except (TypeError, ValueError):
            humor = None
    state, _reaction = note_turn(
        active, active, key, state,
        owner_msg=message or "",
        prev_reply=None,
        prev_meta=state.last_meta,
    )
    ctx = PersonaContext(
        channel="text",
        owner_msg=message or "",
        intent=intent,
        mode=mode,
        urgency=float(urgency or 0.0),
        emotion=emotion,
        receipts=tuple(receipts or ()),
        humor_slider=humor,
        serious_latch_turns_left=state.latch,
        recent_joke_flags=state.recent_joke_flags,
        last_joke_meta=state.last_meta,
        work_state=WorkState(),
        local_time=local_time,
        session_started=(
            datetime.fromisoformat(state.session_started)
            if state.session_started
            else None
        ),
        thread_id=thread_id,
        turn_index=0,
    )
    block = build_persona_block(
        ctx, store=active, weights=state.weights, ledger=active,
        include_core=False, budget=delegation_budget(),
    )
    after_decision(active, key, state, block.tone, None, owner_msg=message or "")
    return block.text.strip()


def delegated_mood_word(
    *,
    intent: str = "general",
    mode: str = "casual",
    urgency: float = 0.0,
    emotion: str = "neutral",
    message: str = "",
) -> str:
    """Persona-backed mood word (kept valid for legacy _MOOD_LINES)."""
    from app.persona.tone import decide_tone

    decision = decide_tone(
        PersonaContext(
            channel="text", owner_msg=message, intent=intent, mode=mode,
            urgency=float(urgency or 0.0), emotion=emotion,
        ),
        None,
    )
    if decision.roast <= 0:
        return "calm_direct"
    if decision.roast == 1:
        return "focused_light"
    return "playful"


def persona_flag_on() -> bool:
    """True when delegation is active: flag on AND dynamic rollback open."""
    try:
        from app.persona.settings import PERSONA_ENABLED
    except Exception:
        return False
    if not PERSONA_ENABLED:
        return False
    try:
        from app.ev.dynamic_personality import dynamic_enabled
    except Exception:
        return False
    return bool(dynamic_enabled())
