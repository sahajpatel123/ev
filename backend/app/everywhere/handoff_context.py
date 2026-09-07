"""G2 C — Bounded cross-device context handoff.

Owner focuses entity on Mac (project/goal/task) -> trusted iPhone can resolve
pronoun "its" to that entity without re-speaking title. Bounded, TTL, versioned,
privacy-respecting, centrally ordered. Once resolved, canonical read still via Core.

 Laws:
 - NEVER sync hidden model/Realtime state — only bounded semantic focus.
 - TTL-bounded (~30 min), ambiguous/stale -> clarification not guess.
 - Multiple writers: server ordering/version bump, last write wins with version.
 - Privacy: sandbox/revoked devices get no owner context.
 - Context != authority: resolved id is re-read from Core for current truth.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, OwnerHandoffContext
from app.utils.text import utcnow

OWNER_KEY = "owner"
TTL_SECONDS = 1800  # 30 min
MAX_RECENT_REFS = 5
MAX_TITLE_LEN = 256

PRONOUNS = frozenset({"it", "its", "it's", "that", "this", "them", "they"})

AMBIGUOUS_MSG = "I want to be precise — which project or goal do you mean?"


def _is_trusted(device: Device | None) -> bool:
    if device is None:
        return False
    if device.revoked_at is not None:
        return False
    return str(getattr(device, "memory_scope", "") or "").lower() != "sandbox"


def _now():
    return utcnow()


def _ensure_aware(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        from datetime import UTC

        return dt.replace(tzinfo=UTC)
    return dt


def _expired(row: OwnerHandoffContext | None) -> bool:
    if row is None or row.expires_at is None:
        return True
    exp = _ensure_aware(row.expires_at)
    now = _ensure_aware(_now())
    return exp <= now


async def get_context(session: AsyncSession) -> OwnerHandoffContext | None:
    row = (
        await session.execute(
            select(OwnerHandoffContext).where(OwnerHandoffContext.owner_key == OWNER_KEY)
        )
    ).scalar_one_or_none()
    if row is None or _expired(row):
        return None
    return row


def public_context(row: OwnerHandoffContext | None) -> dict | None:
    if row is None or _expired(row):
        return None
    return {
        "focused_type": row.focused_type,
        "focused_id": row.focused_id,
        "focused_title": row.focused_title,
        "focused_project_id": row.focused_project_id,
        "focused_project_title": row.focused_project_title,
        "focused_goal_id": row.focused_goal_id,
        "current_task": row.current_task,
        "recent_refs": list(row.recent_refs or []),
        "source_device_id": str(row.source_device_id) if row.source_device_id else None,
        "version": int(row.version or 1),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
    }


async def set_context(
    session: AsyncSession,
    *,
    source_device: Device,
    focused_type: str | None = None,
    focused_id: str | None = None,
    focused_title: str | None = None,
    focused_project_id: str | None = None,
    focused_project_title: str | None = None,
    focused_goal_id: str | None = None,
    current_task: str | None = None,
    recent_refs: list[dict] | None = None,
) -> dict:
    """Centrally mediated context update (C5 version bump)."""
    if not _is_trusted(source_device):
        return {"ok": False, "error_code": "DEVICE_NOT_TRUSTED", "message": "Device not trusted for context"}
    if source_device.revoked_at is not None:
        return {"ok": False, "error_code": "DEVICE_REVOKED"}

    row = (
        await session.execute(
            select(OwnerHandoffContext).where(OwnerHandoffContext.owner_key == OWNER_KEY)
        )
    ).scalar_one_or_none()
    now = _now()
    ttl = timedelta(seconds=TTL_SECONDS)
    if row is None:
        row = OwnerHandoffContext(
            owner_key=OWNER_KEY,
            focused_type=(focused_type or "project")[:32] if focused_type else None,
            focused_id=(focused_id or "")[:128] or None,
            focused_title=(focused_title or "")[:MAX_TITLE_LEN] or None,
            focused_project_id=(focused_project_id or focused_id or "")[:128] or None,
            focused_project_title=(focused_project_title or focused_title or "")[:MAX_TITLE_LEN] or None,
            focused_goal_id=(focused_goal_id or "")[:128] or None,
            current_task=(current_task or "")[:MAX_TITLE_LEN] or None,
            recent_refs=list((recent_refs or [])[:MAX_RECENT_REFS]),
            source_device_id=source_device.id,
            version=1,
            updated_at=now,
            expires_at=now + ttl,
        )
        session.add(row)
        await session.flush()
        return {"ok": True, **(public_context(row) or {})}

    # Update with version bump (server ordering)
    row.focused_type = (focused_type or row.focused_type or "project")[:32] if (focused_type or row.focused_type) else row.focused_type
    if focused_id is not None:
        row.focused_id = focused_id[:128]
    if focused_title is not None:
        row.focused_title = focused_title[:MAX_TITLE_LEN]
    if focused_project_id is not None:
        row.focused_project_id = focused_project_id[:128]
    if focused_project_title is not None:
        row.focused_project_title = focused_project_title[:MAX_TITLE_LEN]
    if focused_goal_id is not None:
        row.focused_goal_id = focused_goal_id[:128]
    if current_task is not None:
        row.current_task = current_task[:MAX_TITLE_LEN]
    if recent_refs is not None:
        row.recent_refs = list(recent_refs[:MAX_RECENT_REFS])
    else:
        # append current focus to recent refs for ambiguity detection
        refs = list(row.recent_refs or [])
        if row.focused_id and row.focused_title:
            entry = {"type": row.focused_type or "project", "id": row.focused_id, "title": row.focused_title}
            if not any(r.get("id") == entry["id"] for r in refs):
                refs.insert(0, entry)
                row.recent_refs = refs[:MAX_RECENT_REFS]
    row.source_device_id = source_device.id
    row.version = int(row.version or 0) + 1
    row.updated_at = now
    row.expires_at = now + ttl
    await session.flush()
    return {"ok": True, **(public_context(row) or {})}


async def clear_context(session: AsyncSession) -> None:
    row = (
        await session.execute(
            select(OwnerHandoffContext).where(OwnerHandoffContext.owner_key == OWNER_KEY)
        )
    ).scalar_one_or_none()
    if row is not None:
        await session.delete(row)
        await session.flush()


def _looks_pronoun(text: str) -> bool:
    low = (text or "").lower()
    # bare field query with pronoun "its" etc without naming entity
    if " its " in f" {low} " or low.strip() in {"what is its priority?", "what is its priority", "what's its priority?"}:
        return True
    # contains pronoun but not the focused title
    if any(p in low for p in (" its ", " it's ", " its?", " its.", " 'it ", " it?")):
        return True
    # explicit priority query without title
    return bool("what is its priority" in low or "what is its priority?" in low or "tell me its priority" in low)


async def resolve_pronoun(
    session: AsyncSession,
    *,
    text: str,
    requesting_device: Device | None,
) -> dict:
    """Try to resolve 'its' etc to focused entity. Returns {resolved, entity, reason}."""
    if requesting_device is not None and not _is_trusted(requesting_device):
        return {"ok": False, "error_code": "DEVICE_NOT_TRUSTED", "clarify": False}
    row = await get_context(session)
    if row is None or _expired(row):
        return {"ok": False, "error_code": "AMBIGUOUS_CONTEXT", "reason": "expired", "clarify": True, "message": AMBIGUOUS_MSG}
    (text or "").lower()
    # If text explicitly names an entity, don't use context (low-confidence override)
    # For now if text contains more than 3 words besides pronoun, treat as potential explicit
    # But spec C4: if two plausible entities, ask clarification.
    refs = list(row.recent_refs or [])
    # Ambiguity: two plausible recent entities with same type
    if len(refs) >= 2:
        # If recent refs have 2+ projects, ambiguous
        [r.get("type") for r in refs]
        project_refs = [r for r in refs if (r.get("type") or "project") == "project"]
        if len(project_refs) >= 2 and _looks_pronoun(text):
            # Check if both could match pronoun: if pronoun query and 2 recent projects
            return {"ok": False, "error_code": "AMBIGUOUS_CONTEXT", "reason": "multiple_candidates", "clarify": True, "message": AMBIGUOUS_MSG, "candidates": project_refs[:2]}

    # High-confidence single focus
    if row.focused_id and row.focused_title:
        # Ensure TTL still valid (half TTL is still high confidence; we check expires_at already)
        upd = _ensure_aware(row.updated_at) or _ensure_aware(_now())
        now_aware = _ensure_aware(_now())
        age = (now_aware - upd).total_seconds() if upd and now_aware else 0
        if age > TTL_SECONDS * 0.9:
            return {"ok": False, "error_code": "AMBIGUOUS_CONTEXT", "reason": "stale", "clarify": True, "message": AMBIGUOUS_MSG}
        return {
            "ok": True,
            "resolved": True,
            "focused_type": row.focused_type,
            "focused_id": row.focused_id,
            "focused_title": row.focused_title,
            "source_device_id": str(row.source_device_id) if row.source_device_id else None,
            "version": row.version,
        }
    return {"ok": False, "error_code": "AMBIGUOUS_CONTEXT", "reason": "no_focus", "clarify": True, "message": AMBIGUOUS_MSG}
