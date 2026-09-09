"""Presence OS V1 — deterministic condition evaluators.

Event-driven first (callers invoke on matching events), bounded schedule
second (frequency_s/TTL enforced by the service). No model calls, no loops.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PresenceCondition

_OPS = ("eq", "ne", "lt", "lte", "gt", "gte", "contains", "in_", "exists")


def _predicate(path: str, op: str, value: Any, context: dict) -> bool:
    cur: Any = context
    for part in (path or "").split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return op == "exists" and False if op == "ne" else False
    if op == "exists":
        return True
    if op == "eq":
        return cur == value
    if op == "ne":
        return cur != value
    try:
        if op == "lt":
            return cur < value
        if op == "lte":
            return cur <= value
        if op == "gt":
            return cur > value
        if op == "gte":
            return cur >= value
        if op == "contains":
            return value in cur
        if op == "in_":
            return cur in (value or [])
    except TypeError:
        return False
    return False


def _now() -> datetime:
    return datetime.now(UTC)


async def _device_states(session: AsyncSession) -> dict[str, str]:
    try:
        from app.everywhere.devices import presence_state
        from app.models import Device

        rows = (
            await session.execute(select(Device).where(Device.revoked_at.is_(None)))
        ).scalars().all()
        out: dict[str, str] = {}
        for d in rows:
            try:
                out[str(d.role or "companion")] = str(presence_state(d))
            except Exception:
                continue
        return out
    except Exception:
        return {}


async def evaluate(
    session: AsyncSession, cond: PresenceCondition, context: dict
) -> bool:
    payload = dict(cond.payload or {})
    cls = str(cond.cond_class or "")
    if cls == "TIME":
        at = payload.get("at")
        if not at:
            return False
        try:
            moment = datetime.fromisoformat(str(at))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
            return _now() >= moment
        except ValueError:
            return False
    if cls in ("DEVICE_ONLINE", "DEVICE_OFFLINE"):
        role = str(payload.get("role") or "home_station")
        states = await _device_states(session)
        state = states.get(role, "OFFLINE")
        online = state in ("ONLINE", "RECENTLY_SEEN", "DEGRADED")
        return online if cls == "DEVICE_ONLINE" else not online
    if cls == "APPROVAL_RESOLVED":
        action_id = str(payload.get("action_id") or "")
        if not action_id:
            return False
        try:
            from app.models import ApprovedAction

            row = await session.get(ApprovedAction, UUID(action_id))
            return row is not None and str(row.status) != "pending"
        except (ValueError, TypeError, AttributeError):
            return False
    if cls == "TASK_COMPLETED":
        return await _task_completed(session, payload)
    if cls == "RESEARCH_RESULT":
        job_id = str(payload.get("job_id") or payload.get("session_id") or "")
        if not job_id:
            return False
        try:
            from uuid import UUID as _UUID

            from app.ev.research import ResearchService

            svc = ResearchService(session, "master")
            job = await svc.detail(_UUID(job_id))
            if job is None:
                return False
            done = str(job.status or "") in ("completed", "concluded")
            return done and bool(list(job.final_artifacts or []) or list(job.citations or []))
        except (ValueError, TypeError, AttributeError):
            return False
    if cls in ("FILE_APPEARED", "FILE_CHANGED"):
        name = str(payload.get("filename") or payload.get("path") or "")
        if not name:
            return False
        try:
            from app.models import Attachment

            rows = (
                await session.execute(
                    select(Attachment).where(Attachment.filename.contains(name[:120]))
                )
            ).scalars().all()
            if not rows:
                return False
            if cls == "FILE_APPEARED":
                return True
            known = str(payload.get("sha256") or "")
            return any(str(getattr(r, "sha256", "") or "") != known for r in rows)
        except Exception:
            return False
    if cls == "LOCATION_REGION":
        region = payload.get("region") or {}
        entered = (context.get("region") or {}) if isinstance(context.get("region"), dict) else {}
        return bool(region) and entered.get("id") == region.get("id") and bool(entered.get("inside"))
    if cls in ("OWNER_STATE", "EXTERNAL_DATA_CHANGE", "DEVICE_OFFLINE", "CUSTOM_PREDICATE"):
        pred = payload.get("predicate") or {}
        if not isinstance(pred, dict) or not pred.get("path"):
            return False
        return _predicate(str(pred.get("path")), str(pred.get("op", "eq")), pred.get("value"), context)
    # --- DIGITAL OPERATIONS V1 (additive) ---
    if cls in {
        "EMAIL_RECEIVED_MATCH",
        "WHATSAPP_REPLY_MATCH",
        "DOCUMENT_RECEIVED",
        "PERSON_RESPONDED",
        "DEADLINE_APPROACHING",
        "CALENDAR_EVENT_CHANGED",
    }:
        from app.digital.conditions import evaluate_digital

        return await evaluate_digital(session, cond, context)
    return False


async def _task_completed(session: AsyncSession, payload: dict) -> bool:
    kind = str(payload.get("kind") or "")
    ref = str(payload.get("ref") or payload.get("action_id") or "")
    if kind == "broker" and ref:
        try:
            from app.everywhere.device_actions import get_action

            row = await get_action(session, ref, owner_scope="master")
            return row is not None and str(row.status) == "SUCCEEDED"
        except Exception:
            return False
    if kind == "research" and ref:
        try:
            from uuid import UUID as _UUID2

            from app.ev.research import ResearchService

            svc = ResearchService(session, "master")
            job = await svc.detail(_UUID2(ref))
            return job is not None and str(job.status or "") in ("completed", "concluded")
        except (ValueError, TypeError, AttributeError):
            return False
    if kind == "contract" and ref:
        try:
            from uuid import UUID as _UUID3

            from app.models import PresenceContract as _PC

            got = (
                await session.execute(select(_PC).where(_PC.id == _UUID3(ref)))
            ).scalars().first()
            return got is not None and str(got.state) == "COMPLETED"
        except (ValueError, TypeError, AttributeError):
            return False
    return False
