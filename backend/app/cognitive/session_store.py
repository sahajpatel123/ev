"""Durable CognitiveSession hosted on Presence GoalContract when possible."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.config import settings

_ACTIVE: CognitiveSession | None = None
_ACTIVE_STAMP: tuple[float, int] | None = None

# The session holds the owner's own words and Evie's replies, so it is a
# private store: same 0700/0600 convention as the memory store, not the
# default umask.
_DIR_MODE = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _path() -> Path:
    root = Path(str(getattr(settings, "storage_root", None) or "storage"))
    folder = root / "cognitive"
    folder.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
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
    recent_turns: list[dict[str, Any]] = field(default_factory=list)
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
            recent_turns=[
                row for row in (raw.get("recent_turns") or []) if isinstance(row, dict)
            ],
            open_questions=list(raw.get("open_questions") or []),
            evidence_refs=list(raw.get("evidence_refs") or []),
            live_session_id=raw.get("live_session_id"),
            waiting=str(raw.get("waiting") or ""),
            updated_at=str(raw.get("updated_at") or ""),
            parked=bool(raw.get("parked")),
        )
    except (TypeError, ValueError):
        return None


def _offer_epoch(offer: Any) -> float:
    """When an offer was asked, as epoch seconds. Missing or bad -> -inf."""

    if not isinstance(offer, dict):
        return float("-inf")
    raw = str(offer.get("at") or "").strip()
    if not raw:
        return float("-inf")
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return float("-inf")
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.timestamp()


def _file_stamp() -> tuple[float, int] | None:
    try:
        stat = _path().stat()
    except OSError:
        return None
    return (stat.st_mtime, stat.st_size)


def forget_live_cache() -> None:
    """Drop in-process cache so the next current() reloads durable JSON."""

    global _ACTIVE, _ACTIVE_STAMP
    _ACTIVE = None
    _ACTIVE_STAMP = None


def reset_for_tests() -> None:
    global _ACTIVE, _ACTIVE_STAMP
    _ACTIVE = None
    _ACTIVE_STAMP = None
    path = _path()
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def current() -> CognitiveSession:
    """The live session, reloaded when another process has written it.

    Mac Talk runs the voice edge on its own port and the API runs the kernel
    on another; both hold this same durable session. Without the stamp check
    an offer armed by one process is invisible to the other, and the owner's
    "yes" arrives with no referent.
    """

    global _ACTIVE, _ACTIVE_STAMP
    stamp = _file_stamp()
    if _ACTIVE is not None and stamp != _ACTIVE_STAMP:
        _ACTIVE = None
    if _ACTIVE is not None:
        return _ACTIVE
    loaded = _load_file()
    if loaded is None:
        loaded = CognitiveSession(session_id=uuid4().hex, updated_at=_now())
    _ACTIVE = loaded
    _ACTIVE_STAMP = stamp
    return loaded


def save(session: CognitiveSession) -> CognitiveSession:
    global _ACTIVE, _ACTIVE_STAMP
    path = _path()
    # Another process may have written while this one held its copy (a live
    # voice turn on the voice edge and a typed turn on the API both do). The
    # turn ledger only ever grows, so union it in rather than erasing the
    # other surface's rows.
    stale = _file_stamp() != _ACTIVE_STAMP
    if stale:
        from app.cognitive.intent import PENDING_OFFER_KEY, RECENT_TURNS_MAX

        other = _load_file()
        if other is not None:
            seen = {str(row.get("at")) for row in session.recent_turns}
            for row in other.recent_turns:
                if str(row.get("at")) not in seen:
                    session.recent_turns.append(row)
            session.recent_turns.sort(key=lambda row: str(row.get("at") or ""))
            session.recent_turns = session.recent_turns[-RECENT_TURNS_MAX:]
            # The offer is the other field both processes touch. Ours may be
            # older than the disk copy, so let the newer question win — and if
            # the other process has answered ours, do not resurrect it.
            theirs = (other.constraints or {}).get(PENDING_OFFER_KEY)
            ours = (session.constraints or {}).get(PENDING_OFFER_KEY)
            if theirs is not None:
                if ours is None or _offer_epoch(theirs) > _offer_epoch(ours):
                    session.constraints[PENDING_OFFER_KEY] = theirs
            elif ours is not None:
                file_written = (_file_stamp() or (0.0, 0))[0]
                if _offer_epoch(ours) < file_written:
                    session.constraints.pop(PENDING_OFFER_KEY, None)

    session.updated_at = _now()
    _ACTIVE = session
    # Replace, never truncate-and-write: the other process reads this file on
    # every turn, and a torn read silently mints a fresh session that drops
    # the offer, the goal, and the ledger.
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(session.public(), indent=2, default=str), encoding="utf-8")
    os.chmod(tmp, _FILE_MODE)
    # Stamp the bytes we actually wrote, taken before the swap. Stamping the
    # path afterwards would cache another writer's stamp against our content
    # and `current()` would stop reloading — a wedge, not just a lost update.
    written = tmp.stat()
    stamp = (written.st_mtime, written.st_size)
    os.replace(tmp, path)
    _ACTIVE_STAMP = stamp
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
