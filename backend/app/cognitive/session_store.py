"""Durable CognitiveSession hosted on Presence GoalContract when possible."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.config import settings

_ACTIVE: CognitiveSession | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _path() -> Path:
    root = Path(str(getattr(settings, "storage_root", None) or "storage"))
    folder = root / "cognitive"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "session.json"


@dataclass
class CognitiveSession:
    session_id: str
    focused_goal_id: str | None = None
    semantic_objective: str = ""
    constraints: dict[str, Any] = field(default_factory=dict)
    prepare_only: bool = False
    steering_version: int = 0
    plan_version: int = 0
    completed_effects: list[dict[str, Any]] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    evidence_refs: list[dict[str, Any]] = field(default_factory=list)
    live_session_id: str | None = None
    waiting: str = ""
    updated_at: str = ""
    parked: bool = False

    def public(self) -> dict[str, Any]:
        return asdict(self)


def _load_file() -> CognitiveSession | None:
    path = _path()
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or not raw.get("session_id"):
        return None
    try:
        return CognitiveSession(
            session_id=str(raw["session_id"]),
            focused_goal_id=raw.get("focused_goal_id"),
            semantic_objective=str(raw.get("semantic_objective") or ""),
            constraints=dict(raw.get("constraints") or {}),
            prepare_only=bool(raw.get("prepare_only")),
            steering_version=int(raw.get("steering_version") or 0),
            plan_version=int(raw.get("plan_version") or 0),
            completed_effects=list(raw.get("completed_effects") or []),
            open_questions=list(raw.get("open_questions") or []),
            evidence_refs=list(raw.get("evidence_refs") or []),
            live_session_id=raw.get("live_session_id"),
            waiting=str(raw.get("waiting") or ""),
            updated_at=str(raw.get("updated_at") or ""),
            parked=bool(raw.get("parked")),
        )
    except (TypeError, ValueError):
        return None


def forget_live_cache() -> None:
    """Drop in-process cache so the next current() reloads durable JSON."""

    global _ACTIVE
    _ACTIVE = None


def reset_for_tests() -> None:
    global _ACTIVE
    _ACTIVE = None
    path = _path()
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def current() -> CognitiveSession:
    global _ACTIVE
    if _ACTIVE is not None:
        return _ACTIVE
    loaded = _load_file()
    if loaded is None:
        loaded = CognitiveSession(session_id=uuid4().hex, updated_at=_now())
    _ACTIVE = loaded
    return loaded


def save(session: CognitiveSession) -> CognitiveSession:
    global _ACTIVE
    session.updated_at = _now()
    _ACTIVE = session
    path = _path()
    path.write_text(json.dumps(session.public(), indent=2, default=str), encoding="utf-8")
    return session


def bind_live(live_session_id: str | None) -> CognitiveSession:
    row = current()
    if live_session_id:
        incoming = str(live_session_id)
        previous = str(row.live_session_id or "")
        if previous and previous != incoming:
            # Closing Evie / a new live conversation is a new mind. Do not
            # keep a leftover file GoalContract just because session.json survived.
            row.live_session_id = incoming
            from app.cognitive.intent import clear_pending_send, release_conversational_work

            release_conversational_work(row, keep_bind=False)
            clear_pending_send(row)
        elif previous != incoming:
            row.live_session_id = incoming
            save(row)
        # Same live id: do not save. save() would refresh updated_at and
        # defeat conversational TTL on a leftover file job.
    return row


def bump_steering(session: CognitiveSession, *, prepare_only: bool | None = None) -> CognitiveSession:
    session.steering_version = int(session.steering_version) + 1
    session.plan_version = int(session.plan_version) + 1
    if prepare_only is not None:
        session.prepare_only = bool(prepare_only)
        session.constraints["prepare_only"] = bool(prepare_only)
    return save(session)


def remember_effect(session: CognitiveSession, effect: dict[str, Any]) -> CognitiveSession:
    blob = dict(effect)
    blob.setdefault("at", _now())
    session.completed_effects.append(blob)
    session.completed_effects = session.completed_effects[-40:]
    if blob.get("ok") or blob.get("verified"):
        session.evidence_refs.append(
            {
                "kind": blob.get("kind") or blob.get("name") or "effect",
                "ok": blob.get("ok"),
                "verified": blob.get("verified"),
                "at": blob.get("at"),
            }
        )
        session.evidence_refs = session.evidence_refs[-40:]
    return save(session)


def status_line(session: CognitiveSession | None = None) -> str:
    row = session or current()
    try:
        from app.cognitive.intent import is_stale

        if is_stale(row):
            return "Nothing is in progress."
    except Exception:
        pass
    if row.parked:
        return f"Parked: {(row.semantic_objective or 'the last task')[:160]}"
    try:
        from app.cognitive.intent import pending_send

        waiting = pending_send(row)
    except Exception:
        waiting = None
    if waiting:
        who = str(waiting.get("to") or "them")
        return f"Waiting for what to say to {who}."
    if row.focused_goal_id or row.semantic_objective:
        extra = " (prepare only)" if row.prepare_only else ""
        return f"Working on {(row.semantic_objective or 'the current goal')[:160]}{extra}."
    return "Nothing is in progress."


def has_active_work(session: CognitiveSession | None = None) -> bool:
    row = session or current()
    try:
        from app.cognitive.intent import is_stale

        if is_stale(row):
            return False
    except Exception:
        pass
    if row.parked:
        return True
    try:
        from app.cognitive.intent import pending_send

        if pending_send(row):
            return True
    except Exception:
        pass
    return bool(row.focused_goal_id or row.semantic_objective)
