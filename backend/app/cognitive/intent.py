"""Turn-start mind: the current utterance is the job.

A finished or abandoned file task must not occupy the next ask. A new live
session, silence past the conversational TTL, or a different domain (send,
look, computer, chat, …) releases leftover file GoalContracts, including
``use_files_act_only`` constraints that would block WhatsApp or a Mac act.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from app.cognitive.session_store import CognitiveSession, bump_steering, save

CONVERSATION_TTL_S = 20 * 60
_TURN_DOMAIN = "turn_domain"
_UTTER_KEY = "owner_utterance"
_LIVE_ROTATED = "_live_rotated"
_DROPPED_GOAL = "_dropped_goal_id"
PENDING_SEND_KEY = "pending_send"

_TRANSIENT_CONSTRAINTS = frozenset(
    {
        "use_files_act_only",
        "background_only",
        "location",
        "no_finder_window",
        "no_focus_steal",
        "no_space_switch",
        "prepare_only",
        "bound_artifact",
        "work_shape",
        _LIVE_ROTATED,
        _DROPPED_GOAL,
    }
)

_LOOK = re.compile(
    r"\b(?:look(?:\s+at)?|see this|what(?:'s| is) this|show me this|"
    r"what am i holding|camera)\b",
    re.I,
)
_SEND_HINT = re.compile(
    r"\b(?:send|whatsapp|text|txt|message|email|e-?mail|imessage)\b",
    re.I,
)


def classify_domain(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return "open"
    try:
        from app.ev.send_intent import incomplete_send_recipient, parse_send_intent

        if parse_send_intent(raw) is not None or incomplete_send_recipient(raw):
            return "send"
    except Exception:
        pass
    messaging_named = bool(
        re.search(
            r"\b(?:whatsapp|imessage|i-message|sms|rcs|e-?mail|gmail)\b",
            raw,
            re.I,
        )
    )
    if _SEND_HINT.search(raw) and re.search(r"\b(?:to|saying|that says)\b", raw, re.I):
        fileish = False
        try:
            from app.ev.laptop_files import looks_like_file_task

            fileish = looks_like_file_task(raw)
        except Exception:
            fileish = False
        if messaging_named or not fileish:
            return "send"
    if _LOOK.search(raw):
        return "look"
    try:
        from app.ev.computer_strategy import looks_like_app_or_web_task

        if looks_like_app_or_web_task(raw):
            return "computer"
    except Exception:
        pass
    try:
        from app.cognitive.artifact import classify_owner_work

        if classify_owner_work(raw) == "one_artifact":
            return "file"
    except Exception:
        pass
    return "open"


def pending_send(session: CognitiveSession) -> dict[str, Any] | None:
    raw = session.constraints.get(PENDING_SEND_KEY)
    if not isinstance(raw, dict):
        return None
    to = str(raw.get("to") or "").strip()
    if not to:
        return None
    out: dict[str, Any] = {"to": to}
    channel = str(raw.get("channel") or "").strip().lower()
    if channel:
        out["channel"] = channel
    return out


def set_pending_send(
    session: CognitiveSession,
    *,
    to: str,
    channel: str | None = None,
) -> CognitiveSession:
    payload: dict[str, Any] = {"to": (to or "").strip()}
    wanted = (channel or "").strip().lower()
    if wanted:
        payload["channel"] = wanted
    session.constraints[PENDING_SEND_KEY] = payload
    session.waiting = "send_body"
    return save(session)


def clear_pending_send(session: CognitiveSession) -> CognitiveSession:
    session.constraints.pop(PENDING_SEND_KEY, None)
    if session.waiting == "send_body":
        session.waiting = ""
    return save(session)


def is_stale(session: CognitiveSession) -> bool:
    raw = str(session.updated_at or "").strip()
    if not raw:
        return False
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return (datetime.now(UTC) - stamp).total_seconds() > CONVERSATION_TTL_S


def release_conversational_work(
    session: CognitiveSession,
    *,
    keep_bind: bool = False,
) -> str | None:
    """Drop leftover GoalContract / file-only constraints. Return dropped id."""

    dropped = str(session.focused_goal_id or "") or None
    session.focused_goal_id = None
    session.semantic_objective = ""
    session.parked = False
    session.prepare_only = False
    session.waiting = ""
    for key in list(session.constraints):
        if keep_bind and key == "bound_artifact":
            continue
        if key in _TRANSIENT_CONSTRAINTS or str(key).startswith("_"):
            session.constraints.pop(key, None)
    if not keep_bind:
        session.completed_effects = [
            row
            for row in session.completed_effects
            if str(row.get("kind") or "") not in {"files.act", "computer.perform_effect"}
        ][-8:]
    bump_steering(session)
    if dropped:
        session.constraints[_DROPPED_GOAL] = dropped
        save(session)
    return dropped


def begin_owner_turn(cognition: CognitiveSession, text: str) -> CognitiveSession:
    """Make this utterance the live job. Stale or other-domain work is released."""

    raw = (text or "").strip()
    rotated = bool(cognition.constraints.pop(_LIVE_ROTATED, False))
    domain = classify_domain(raw)
    bound = dict(cognition.constraints.get("bound_artifact") or {})
    follow = bool(bound) and _is_file_followup(raw, bound) and domain in {"file", "open"}
    leftover = bool(
        cognition.constraints.get("use_files_act_only")
        or cognition.semantic_objective
        or cognition.focused_goal_id
        or bound
    )
    stale = is_stale(cognition)
    if rotated or stale or (leftover and not follow):
        # TTL expiry drops the bind too — "add eggs" twelve hours later is
        # not the live job. Same-session follow-ups inside the TTL keep it.
        release_conversational_work(
            cognition,
            keep_bind=follow and not rotated and not stale,
        )
        if rotated or stale:
            clear_pending_send(cognition)
        if follow and not rotated and not stale:
            bound = dict(cognition.constraints.get("bound_artifact") or {})
        else:
            bound = {}
            follow = False

    if follow and not rotated:
        cognition.constraints[_UTTER_KEY] = raw[:2000]
        cognition.constraints[_TURN_DOMAIN] = "file"
        return save(cognition)

    from app.cognitive.artifact import classify_owner_work

    cognition.constraints[_TURN_DOMAIN] = domain
    cognition.constraints[_UTTER_KEY] = raw[:2000]
    cognition.constraints["work_shape"] = classify_owner_work(raw)
    if domain != "file":
        cognition.constraints.pop("bound_artifact", None)
        cognition.constraints.pop("use_files_act_only", None)
    if domain in {"look", "file"}:
        clear_pending_send(cognition)
    return save(cognition)


def take_dropped_goal_id(cognition: CognitiveSession) -> str | None:
    raw = cognition.constraints.pop(_DROPPED_GOAL, None)
    if raw:
        save(cognition)
    return str(raw) if raw else None


def close_finished_file_goal(cognition: CognitiveSession) -> CognitiveSession:
    """A verified one-file write is done. Keep the bind for short follow-ups."""

    cognition.semantic_objective = ""
    cognition.focused_goal_id = None
    cognition.prepare_only = False
    cognition.constraints.pop("use_files_act_only", None)
    return save(cognition)


def _is_file_followup(text: str, bound: dict[str, Any]) -> bool:
    raw = (text or "").strip()
    path = str(bound.get("path") or "")
    if not raw or not path:
        return False
    try:
        from app.ev.computer_strategy import looks_like_app_or_web_task

        if looks_like_app_or_web_task(raw):
            return False
    except Exception:
        pass
    try:
        from app.cognitive.artifact import _is_followup

        return bool(_is_followup(raw, bound))
    except Exception:
        return False
