"""Owner-scoped assistant identity: nickname, greeting, live thread, dedication."""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import AssistantProfile, ConversationThread, Entity, Event, OwnerIdentity
from app.schemas import EventCreate
from app.utils.text import utcnow

DEFAULT_NICKNAME = "EVIE"
NICKNAME_MAX = 40
DEDICATION_MAX_CHARS = 500

_RESET_NAMES = frozenset({"evie", "ev", "e.v.", "go back to evie", "reset"})


def spoken_name(name: str | None) -> str:
    """Canonical spoken nickname. Unset / EV / E.V. collapse to EVIE."""

    raw = (name or "").strip()
    if not raw or raw.upper() in {"EV", "E.V."}:
        return DEFAULT_NICKNAME
    return raw


def greeting_line(owner_preferred_name: str | None) -> str:
    name = (owner_preferred_name or "").strip()
    if name:
        return f"Welcome back, {name}."
    return "Welcome back."


def is_reset_name(name: str) -> bool:
    return (name or "").strip().lower() in _RESET_NAMES


@dataclass(frozen=True)
class NicknameDecision:
    ok: bool
    name: str
    reason: str | None = None


def validate_nickname(
    name: str,
    *,
    owner_names: list[str] | None = None,
    people_names: list[str] | None = None,
) -> NicknameDecision:
    """Refuse empty names and impersonation of the owner or a known person."""

    cleaned = (name or "").strip()
    if not cleaned:
        return NicknameDecision(False, cleaned, "empty")
    if len(cleaned) > NICKNAME_MAX:
        return NicknameDecision(False, cleaned, "too_long")
    lowered = cleaned.lower()
    for owner in owner_names or []:
        if owner and lowered == owner.strip().lower():
            return NicknameDecision(False, cleaned, "impersonates_owner")
    for person in people_names or []:
        if person and lowered == person.strip().lower():
            return NicknameDecision(False, cleaned, "impersonates_third_party")
    return NicknameDecision(True, cleaned)


async def get_profile(session: AsyncSession) -> AssistantProfile:
    row = (
        await session.execute(
            select(AssistantProfile).order_by(AssistantProfile.created_at.asc()).limit(1)
        )
    ).scalars().first()
    if row is not None:
        return row
    owner = (
        await session.execute(
            select(OwnerIdentity).order_by(OwnerIdentity.created_at.asc()).limit(1)
        )
    ).scalars().first()
    preferred = None
    if owner is not None and owner.display_name:
        preferred = owner.display_name.strip() or None
    row = AssistantProfile(
        owner_id=owner.id if owner is not None else None,
        nickname=DEFAULT_NICKNAME,
        owner_preferred_name=preferred,
        greeting_enabled=True,
    )
    session.add(row)
    await session.flush()
    return row


async def impersonation_names(session: AsyncSession) -> tuple[list[str], list[str]]:
    profile = await get_profile(session)
    owner_names: list[str] = []
    if profile.owner_preferred_name:
        owner_names.append(profile.owner_preferred_name)
    owner = (
        await session.execute(
            select(OwnerIdentity).order_by(OwnerIdentity.created_at.asc()).limit(1)
        )
    ).scalars().first()
    if owner is not None and owner.display_name:
        owner_names.append(owner.display_name)
    people = (
        await session.execute(select(Entity.name).where(Entity.entity_type == "person"))
    ).scalars().all()
    return owner_names, [name for name in people if name]


async def set_nickname(session: AsyncSession, name: str) -> NicknameDecision:
    if is_reset_name(name):
        return await reset_nickname(session)
    owner_names, people_names = await impersonation_names(session)
    decision = validate_nickname(name, owner_names=owner_names, people_names=people_names)
    if not decision.ok:
        return decision
    profile = await get_profile(session)
    profile.nickname = decision.name
    profile.updated_at = utcnow()
    await session.flush()
    return decision


async def reset_nickname(session: AsyncSession) -> NicknameDecision:
    profile = await get_profile(session)
    profile.nickname = DEFAULT_NICKNAME
    profile.updated_at = utcnow()
    await session.flush()
    return NicknameDecision(True, DEFAULT_NICKNAME)


async def set_owner_preferred_name(session: AsyncSession, name: str | None) -> AssistantProfile:
    profile = await get_profile(session)
    profile.owner_preferred_name = (name or "").strip() or None
    profile.updated_at = utcnow()
    await session.flush()
    return profile


async def bind_live_thread(session: AsyncSession) -> ConversationThread:
    """Create or reuse the owner's one live conversation thread."""

    from app.ev import conversation

    profile = await get_profile(session)
    if profile.live_conversation_id is not None:
        thread = await session.get(ConversationThread, profile.live_conversation_id)
        if thread is not None:
            return thread
    thread = await conversation.get_default_thread(session)
    profile.live_conversation_id = thread.id
    profile.updated_at = utcnow()
    await session.flush()
    return thread


async def resolve_live_thread(
    session: AsyncSession,
    conversation_id: UUID | None,
) -> ConversationThread:
    from app.ev import conversation

    if conversation_id is not None:
        return await conversation.resolve_thread(session, conversation_id)
    return await bind_live_thread(session)


async def compile_identity(session: AsyncSession, *, compact: bool = False) -> str:
    """Immutable system prefix: nickname + pinned personality sliders."""

    from app.ev.personality import get_current, identity_block, to_dict

    profile = await get_profile(session)
    sliders = await get_current(session)
    block = identity_block(
        profile.nickname,
        settings.persona_description,
        to_dict(sliders),
        compact=compact,
    )
    last = await last_calibration_report(session)
    if last is not None:
        checks = last.get("checks") or []
        gateway = next((c for c in checks if c.get("name") == "chat_gateway"), None)
        if gateway and gateway.get("status") in {"failed", "degraded"}:
            block += (
                "\nThe chat gateway is down. Say so. "
                "Do not invent a witty substitute."
            )
    return block


async def last_calibration_report(session: AsyncSession) -> dict | None:
    from app.models import CalibrationReportRow

    row = (
        await session.execute(
            select(CalibrationReportRow)
            .order_by(CalibrationReportRow.generated_at.desc())
            .limit(1)
        )
    ).scalars().first()
    if row is None:
        return None
    return dict(row.report or {})


async def cache_calibration(session: AsyncSession, report) -> None:
    from app.models import CalibrationReportRow

    payload = report.model_dump(mode="json") if hasattr(report, "model_dump") else dict(report)
    row = CalibrationReportRow(
        generated_at=report.generated_at if hasattr(report, "generated_at") else utcnow(),
        overall=getattr(report, "overall", payload.get("overall") or "ok"),
        report=payload,
    )
    session.add(row)
    await session.flush()


def worst_check(report: dict) -> dict | None:
    checks = list(report.get("checks") or [])
    failed = [c for c in checks if c.get("status") == "failed"]
    degraded = [c for c in checks if c.get("status") == "degraded"]
    pool = failed or degraded
    return pool[0] if pool else None


def malfunction_line(report: dict | None) -> str | None:
    if not report:
        return None
    worst = worst_check(report)
    if worst is None:
        return None
    name = str(worst.get("name") or "system")
    return f"I may be malfunctioning: {name}."


async def maybe_text_greeting(
    session: AsyncSession,
    thread_id: UUID,
    *,
    actor: str = "assistant",
) -> str | None:
    """One welcome line as a system-side assistant event on a brand-new thread."""

    from app.ev import conversation
    from app.services.event_service import EventService

    profile = await get_profile(session)
    if not profile.greeting_enabled:
        return None
    history = await conversation.history(session, thread_id, limit=5, access="user")
    if history:
        return None
    existing = (
        await session.execute(
            select(Event.id).where(
                Event.conversation_id == thread_id,
                Event.event_type == "assistant.greeting",
                Event.tombstoned_at.is_(None),
            ).limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return None
    line = greeting_line(profile.owner_preferred_name)
    await EventService(session, actor=actor).create(
        EventCreate(
            source="assistant",
            event_type="assistant.greeting",
            text=line,
            conversation_id=thread_id,
            metadata={"kind": "greeting", "system": True},
        )
    )
    return line


async def emit_awake_greeting(
    session: AsyncSession,
    *,
    thread_id: UUID,
    actor: str = "voice",
) -> str | None:
    profile = await get_profile(session)
    if not profile.greeting_enabled:
        return None
    line = greeting_line(profile.owner_preferred_name)
    from app.services.event_service import EventService

    await EventService(session, actor=actor).create(
        EventCreate(
            source="voice",
            event_type="assistant.greeting",
            text=line,
            conversation_id=thread_id,
            metadata={"kind": "greeting", "system": True},
        )
    )
    return line


async def set_dedication(
    session: AsyncSession,
    *,
    text: str | None = None,
    blob_id: str | None = None,
) -> AssistantProfile:
    cleaned = (text or "").strip() or None
    if cleaned and len(cleaned) > DEDICATION_MAX_CHARS:
        raise ValueError(f"Dedication text must be ≤{DEDICATION_MAX_CHARS} characters")
    profile = await get_profile(session)
    if cleaned is not None:
        profile.dedication_text = cleaned
    if blob_id is not None:
        profile.dedication_blob_id = blob_id.strip() or None
    profile.updated_at = utcnow()
    await session.flush()
    return profile


async def play_dedication(session: AsyncSession, *, auto: bool = False) -> dict:
    profile = await get_profile(session)
    if auto and profile.dedication_played_at is not None:
        return {
            "played": False,
            "reason": "already_played",
            "text": profile.dedication_text,
            "blob_id": profile.dedication_blob_id,
        }
    if not profile.dedication_text and not profile.dedication_blob_id:
        return {"played": False, "reason": "unset", "text": None, "blob_id": None}
    if auto:
        profile.dedication_played_at = utcnow()
        profile.updated_at = utcnow()
        await session.flush()
    return {
        "played": True,
        "reason": "auto" if auto else "on_demand",
        "text": profile.dedication_text,
        "blob_id": profile.dedication_blob_id,
    }


SET_NAME_RE = re.compile(
    r"^\s*(?:call yourself|your name is|set your name to|go by)\s+(.+?)\s*$",
    re.IGNORECASE,
)
RESET_NAME_RE = re.compile(
    r"^\s*(?:go back to evie|reset your name|call yourself evie again)\s*$",
    re.IGNORECASE,
)
QUIET_UNTIL_RE = re.compile(
    r"\b(?:go quiet|quiet hours|be quiet|stay quiet)\s+until\s+(\d{1,2}(?::\d{2})?)\b",
    re.IGNORECASE,
)
QUIET_RANGE_RE = re.compile(
    r"\bquiet hours\s+(\d{1,2}(?::\d{2})?)\s*(?:to|-|–)\s*(\d{1,2}(?::\d{2})?)\b",
    re.IGNORECASE,
)
WHAT_HAPPENED_RE = re.compile(
    r"\b(?:what just happened|what happened|replay (?:that|the last)|last callouts?)\b",
    re.IGNORECASE,
)
PLAY_DEDICATION_RE = re.compile(
    r"\b(?:play (?:the )?dedication|read (?:the )?dedication)\b",
    re.IGNORECASE,
)
SET_DEDICATION_RE = re.compile(
    r"^\s*(?:set (?:the )?dedication(?: to)?|dedication is)\s+(.+?)\s*$",
    re.IGNORECASE,
)
START_WHEELS_RE = re.compile(r"\bstart training wheels\b", re.IGNORECASE)
COMPLETE_WHEELS_RE = re.compile(r"\b(?:finish|complete) training wheels\b", re.IGNORECASE)
PERSONALITY_RE = re.compile(
    r"\b(?:be funnier|more humor|less formal|more formal|more concise|"
    r"less verbose|more verbose|be briefer|more direct)\b",
    re.IGNORECASE,
)


def match_companion_intent(message: str) -> tuple[str, dict] | None:
    text = (message or "").strip()
    if not text:
        return None
    if RESET_NAME_RE.search(text):
        return "reset_name", {}
    match = SET_NAME_RE.search(text)
    if match:
        return "set_name", {"name": match.group(1).strip().strip("\"'")}
    from app.ev.protocols import is_capability_intent, is_refused_ask

    if is_capability_intent(text):
        return "protocols", {"include_refused": is_refused_ask(text)}
    if WHAT_HAPPENED_RE.search(text):
        return "list_callouts", {"limit": 8}
    if PLAY_DEDICATION_RE.search(text):
        return "play_dedication", {}
    match = SET_DEDICATION_RE.search(text)
    if match:
        return "set_dedication", {"text": match.group(1).strip()}
    if START_WHEELS_RE.search(text):
        return "start_training_wheels", {}
    if COMPLETE_WHEELS_RE.search(text):
        return "complete_training_wheels", {}
    match = QUIET_RANGE_RE.search(text)
    if match:
        return "set_quiet_hours", {"start": match.group(1), "end": match.group(2)}
    match = QUIET_UNTIL_RE.search(text)
    if match:
        return "set_quiet_hours", {"until": match.group(1)}
    if PERSONALITY_RE.search(text):
        return "update_personality", {"phrase": text.lower()}
    return None


def _personality_from_phrase(phrase: str, current: dict) -> dict:
    data = dict(current)
    lowered = phrase.lower()
    if "funnier" in lowered or "more humor" in lowered:
        data["humor"] = min(5, int(data.get("humor", 2)) + 2)
    if "more formal" in lowered:
        data["formality"] = min(5, int(data.get("formality", 2)) + 1)
    if "less formal" in lowered:
        data["formality"] = max(1, int(data.get("formality", 2)) - 1)
    if "more concise" in lowered or "less verbose" in lowered or "briefer" in lowered:
        data["verbosity"] = max(1, int(data.get("verbosity", 3)) - 1)
    if "more verbose" in lowered:
        data["verbosity"] = min(5, int(data.get("verbosity", 3)) + 1)
    if "more direct" in lowered:
        data["directness"] = min(5, int(data.get("directness", 3)) + 1)
    return data


async def handle_local_intent(session: AsyncSession, message: str) -> dict | None:
    """Deterministic companion turns that must not wait on the gateway."""

    from app.ev.interaction import ROMANTIC_REFUSAL, romantic_replacement_refused
    from app.ev.personality import get_current, to_dict, update
    from app.schemas import PersonalityUpdate

    if romantic_replacement_refused(message):
        return {"reply": ROMANTIC_REFUSAL, "kind": "refuse", "surfaces": None}

    match = match_companion_intent(message)
    if match is None:
        return None
    intent, args = match
    if intent == "set_name":
        decision = await set_nickname(session, str(args["name"]))
        if not decision.ok:
            return {
                "reply": f"I won't take that name ({decision.reason}).",
                "kind": "set_name",
                "ok": False,
                "reason": decision.reason,
            }
        return {"reply": f"I'll go by {decision.name}.", "kind": "set_name", "ok": True, "name": decision.name}
    if intent == "reset_name":
        decision = await reset_nickname(session)
        return {"reply": f"I'll go by {decision.name}.", "kind": "reset_name", "ok": True, "name": decision.name}
    if intent == "protocols":
        from app.ev.protocols import capability_reply

        payload = await capability_reply(session, include_refused=bool(args.get("include_refused")))
        return {"reply": payload["reply"], "kind": "protocols", "hud": payload["hud"], "surfaces": payload["hud"]}
    if intent == "list_callouts":
        from app.ev.callouts import list_callouts, replay_text

        rows = await list_callouts(session, limit=int(args.get("limit") or 8))
        return {
            "reply": replay_text(rows),
            "kind": "callouts",
            "callouts": [row.text for row in rows],
        }
    if intent == "play_dedication":
        played = await play_dedication(session, auto=False)
        if not played.get("played"):
            return {"reply": "No dedication is stored yet.", "kind": "dedication", **played}
        return {
            "reply": played.get("text") or "Playing the dedication.",
            "kind": "dedication",
            **played,
        }
    if intent == "set_dedication":
        try:
            await set_dedication(session, text=str(args.get("text") or ""))
        except ValueError as exc:
            return {"reply": str(exc), "kind": "dedication", "ok": False}
        return {"reply": "Dedication saved.", "kind": "dedication", "ok": True}
    if intent == "start_training_wheels":
        from app.ev.protocols import start_training_wheels

        payload = await start_training_wheels(session)
        return {"reply": payload["reply"], "kind": "training_wheels", **payload}
    if intent == "complete_training_wheels":
        from app.ev.protocols import complete_training_wheels

        payload = await complete_training_wheels(session)
        return {"reply": payload["reply"] or "Training wheels complete.", "kind": "training_wheels", **payload}
    if intent == "set_quiet_hours":
        from app.notify.proactive import persist_quiet_hours, set_quiet_hours

        hours = set_quiet_hours(until=args.get("until"), start=args.get("start"), end=args.get("end"))
        await persist_quiet_hours(session)
        return {
            "reply": f"Quiet until {hours['end']}. I won't speak unless it's an emergency.",
            "kind": "quiet_hours",
            **hours,
        }
    if intent == "update_personality":
        current = await get_current(session)
        patch = _personality_from_phrase(str(args.get("phrase") or ""), to_dict(current))
        updated = await update(session, PersonalityUpdate(**patch, reason_for_change="voice"))
        return {
            "reply": (
                f"Updated. humor={updated.humor} formality={updated.formality} "
                f"verbosity={updated.verbosity}."
            ),
            "kind": "personality",
            "profile": to_dict(updated),
        }
    return None
