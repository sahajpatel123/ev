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


PENDING_OFFER_KEY = "pending_offer"
PENDING_OFFER_TTL_S = 600.0
RECENT_TURNS_KEY = "recent_turns"
RECENT_TURNS_MAX = 6
_RECENT_TURN_CHARS = 1200

_OFFER_RE = re.compile(
    r"\b(?:do you want me to|want me to|should i|shall i|would you like me to|"
    r"would you like|may i|can i|should we)\b",
    re.I,
)


def _read_aloud_offer(text: str) -> bool:
    """True when the offer itself is to speak an artifact through."""

    try:
        from app.ev.spark_task import wants_readout

        return bool(wants_readout(text))
    except Exception:
        return False


def looks_like_offer(spoken: str | None) -> bool:
    """True when an assistant line asks the owner to confirm a next action.

    The test is on the END of the line, not its length: a mail summary that
    runs to a paragraph and then asks "do you want me to read out the full
    mail?" is still an offer, and capping the whole string used to throw the
    ask away exactly when Evie had said the most.
    """

    text = (spoken or "").strip()
    if not text:
        return False
    text = text[:4000]
    tail = text[-600:]
    if _OFFER_RE.search(tail):
        return True
    return text.rstrip().endswith("?")


def set_pending_offer(
    session: CognitiveSession,
    spoken: str | None,
    *,
    action: dict[str, Any] | None = None,
    ttl_seconds: float = PENDING_OFFER_TTL_S,
) -> CognitiveSession:
    """Remember an assistant offer so the next yes/no binds to it.

    The offer keeps the assistant's own words (the referent a short "yes"
    answers) plus the semantic action that produced them, so an affirmative
    can be carried out on any channel instead of becoming a topic-free
    greeting.
    """

    text = _scrub((spoken or "").strip())
    if not text or not looks_like_offer(text):
        return session
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "text": text[:2000],
        "at": now.isoformat(),
        "expires_at": now.timestamp() + float(ttl_seconds),
    }
    if isinstance(action, dict) and action.get("tool"):
        raw_args = dict(action.get("args") or {})
        payload["action"] = {
            "tool": str(action.get("tool") or "")[:120],
            "args": {
                str(key): (_scrub(str(value)) if isinstance(value, str) else value)
                for key, value in raw_args.items()
            },
        }
    if _read_aloud_offer(text):
        payload["readout"] = True
    session.constraints[PENDING_OFFER_KEY] = payload
    session.waiting = "offer_pending"
    return save(session)


def pending_offer(session: CognitiveSession) -> dict[str, Any] | None:
    raw = session.constraints.get(PENDING_OFFER_KEY)
    if not isinstance(raw, dict):
        return None
    text = str(raw.get("text") or "").strip()
    if not text:
        return None
    try:
        expires = raw.get("expires_at")
        if expires is not None and datetime.now(UTC).timestamp() > float(expires):
            return None
    except (TypeError, ValueError):
        pass
    found: dict[str, Any] = {"text": text}
    if raw.get("at"):
        found["at"] = raw.get("at")
    if isinstance(raw.get("action"), dict):
        found["action"] = raw["action"]
    if raw.get("readout"):
        found["readout"] = True
    return found


def clear_pending_offer(session: CognitiveSession) -> CognitiveSession:
    if PENDING_OFFER_KEY in session.constraints:
        session.constraints.pop(PENDING_OFFER_KEY, None)
        if session.waiting == "offer_pending":
            session.waiting = ""
        return save(session)
    return session


def _scrub(text: str) -> str:
    """Redact credential-like content before it is written to durable state.

    The ledger and the offer are re-injected into the model prompt on later
    turns, and they outlive the input filter that would have redacted a
    credential for the turn it arrived on. Scrubbing at the write keeps a
    secret from being parked in a file and re-sent from there.
    """

    try:
        from app.security.boundary import redact_secrets

        return redact_secrets(text)
    except Exception:  # pragma: no cover - import guard
        return text


def remember_exchange(
    session: CognitiveSession,
    *,
    owner: str,
    assistant: str,
    kind: str = "",
) -> CognitiveSession:
    """Append one owner/Evie exchange to the durable turn ledger.

    Every surface compiles its prompt from this session, so a bounded ledger
    is what lets a bare "yes", "that one", or "read it" resolve against what
    Evie actually just said — on voice, Mac, iPhone, PWA, or text — without a
    bespoke per-feature state store.
    """

    said = _scrub((owner or "").strip())
    replied = _scrub((assistant or "").strip())
    if not said and not replied:
        return session
    session.recent_turns.append(
        {
            "owner": said[:_RECENT_TURN_CHARS],
            "evie": replied[:_RECENT_TURN_CHARS],
            "kind": str(kind or "")[:60],
            "at": datetime.now(UTC).isoformat(),
        }
    )
    session.recent_turns = session.recent_turns[-RECENT_TURNS_MAX:]
    return save(session)


def recent_exchanges(session: CognitiveSession) -> list[dict[str, Any]]:
    rows = session.recent_turns if isinstance(session.recent_turns, list) else []
    return [row for row in rows if isinstance(row, dict)][-RECENT_TURNS_MAX:]


def readout_offer_live(session: CognitiveSession | None = None) -> bool:
    """True when Evie has offered to read an artifact out and it is unanswered."""

    row = session
    if row is None:
        try:
            from app.cognitive.session_store import current

            row = current()
        except Exception:
            return False
    offer = pending_offer(row) if row is not None else None
    return bool(offer and offer.get("readout"))


def continuation_readout(query: str, session: CognitiveSession | None = None) -> bool:
    """True when a short reply answers a live offer to read an artifact aloud.

    The owner answers "Do you want me to read out the full mail?" with "yes".
    That word carries no read-aloud cue of its own, so the live offer decides
    the manner instead of the follow-up utterance.
    """

    raw = (query or "").strip()
    if not raw:
        return False
    if _read_aloud_offer(raw):
        return True
    try:
        from app.ev.continuity import is_affirmative_reply

        if not is_affirmative_reply(raw):
            return False
    except Exception:
        return False
    row = session
    if row is None:
        try:
            from app.cognitive.session_store import current

            row = current()
        except Exception:
            return False
    offer = pending_offer(row)
    return bool(offer and offer.get("readout"))



_SMALL_TALK_ONLY = re.compile(
    r"^(?:"
    r"hi|hello|hey|yo|hiya|hii|"
    r"good\s+(?:morning|afternoon|evening|night)|"
    r"morning|afternoon|evening|"
    r"how\s+are\s+you|how'?s\s+it\s+going|how\s+are\s+things|"
    r"thanks|thank\s+you|thankyou|thx|cool|nice|great|awesome|"
    r"you\s+there|are\s+you\s+there|u\s+there|"
    r"evie|ev"
    r")"
    # A greeting is short, and may name who it is aimed at: "hey there",
    # "hi Evie", "good morning Evie".
    r"(?:[\s,]+(?:there|evie|ev|buddy|mate|dear|sir|madam))*"
    r"[\s,.!?]*$",
    re.IGNORECASE,
)


def is_substantive_turn(text: str) -> bool:
    """False for greetings, pleasantries, and a bare wake word.

    The Mac client sends a synthetic "Hi." when a live conversation opens.
    Treating that as a real turn used to clear the live offer and replace it
    with a greeting, which is exactly how the owner's "yes" ended up
    answering "I am here, what would you like me to do?" instead of the mail
    question Evie had just asked.
    """

    raw = (text or "").strip()
    if not raw:
        return False
    return not bool(_SMALL_TALK_ONLY.match(raw))


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
