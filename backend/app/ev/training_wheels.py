"""Training Wheels checklist, FeatureGate seed, and dispatcher refuse path."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.protocols import REFUSED_PROTOCOLS, set_gate
from app.models import FeatureGate
from app.utils.text import utcnow

TRAINING_STEPS: tuple[str, ...] = (
    "mic_permission",
    "speaker_enroll",
    "quiet_hours",
    "first_calibrate",
    "first_hud",
)

STEP_TITLES = {
    "mic_permission": "Microphone permission",
    "speaker_enroll": "Speaker enrollment",
    "quiet_hours": "Quiet hours set",
    "first_calibrate": "First calibration",
    "first_hud": "First HUD shown",
}

UNLOCK_ON_COMPLETE: tuple[str, ...] = (
    "life.call",
    "actuator.software",
    "actuator.home",
    "maker.queue",
)

ALWAYS_LOCKED: tuple[str, ...] = ("actuator.drone",)

PERMISSION_GATES: dict[str, str] = {
    "phone:act": "life.call",
    "actuator:software": "actuator.software",
    "home:act": "actuator.home",
    "actuator:drone": "actuator.drone",
    "maker:queue": "maker.queue",
}

ACTUATOR_PERMISSIONS = frozenset(PERMISSION_GATES)

_GATES_UNAVAILABLE = False


async def ensure_seed_gates(session: AsyncSession) -> None:
    """Seed refused §2 items and locked fleet gates. Never unlock Instant Kill."""

    global _GATES_UNAVAILABLE
    try:
        for key, _title, detail in REFUSED_PROTOCOLS:
            row = await _gate(session, key)
            if row is None:
                await _insert_gate(session, key, "refused", reason=detail)
            elif row.status != "refused":
                row.status = "refused"
                row.reason = detail
                row.updated_at = utcnow()
        for key in UNLOCK_ON_COMPLETE:
            if await _gate(session, key) is None:
                await _insert_gate(
                    session,
                    key,
                    "locked",
                    reason="Complete Training Wheels to unlock.",
                    setup_hint="Say start training wheels.",
                )
        if await _gate(session, "actuator.drone") is None:
            await _insert_gate(
                session,
                "actuator.drone",
                "locked",
                reason="Hobby drone adapter is not configured (item 45).",
                setup_hint="Configure a drone adapter after Training Wheels.",
            )
        if await _gate(session, "training_wheels") is None:
            await _insert_gate(
                session,
                "training_wheels",
                "locked",
                reason="Say start training wheels.",
            )
        _GATES_UNAVAILABLE = False
        await session.flush()
    except Exception:
        _GATES_UNAVAILABLE = True


async def _insert_gate(
    session: AsyncSession,
    key: str,
    status: str,
    *,
    reason: str | None = None,
    setup_hint: str | None = None,
) -> None:
    try:
        async with session.begin_nested():
            await set_gate(session, key, status, reason=reason, setup_hint=setup_hint)
            await session.flush()
    except IntegrityError:
        return


async def _gate(session: AsyncSession, key: str) -> FeatureGate | None:
    try:
        return (
            await session.execute(select(FeatureGate).where(FeatureGate.key == key))
        ).scalar_one_or_none()
    except Exception:
        return None


def _steps_map(profile) -> dict[str, Any]:
    raw = getattr(profile, "training_steps", None) or {}
    return dict(raw) if isinstance(raw, dict) else {}


async def remaining_steps(session: AsyncSession) -> list[str]:
    from app.ev.assistant import get_profile

    profile = await get_profile(session)
    done = _steps_map(profile)
    return [step for step in TRAINING_STEPS if not done.get(step)]


async def list_locked(session: AsyncSession) -> dict:
    await ensure_seed_gates(session)
    remaining = await remaining_steps(session)
    rows = list((await session.execute(select(FeatureGate))).scalars().all())
    locked = [
        {"key": row.key, "status": row.status, "reason": row.reason}
        for row in rows
        if row.status in {"locked", "refused"}
    ]
    refused = [row.key for row in rows if row.status == "refused"]
    spoken = (
        "Locked: "
        + "; ".join(
            f"{item['key']} ({item['status']})" for item in locked[:12]
        )
        if locked
        else "Nothing is locked."
    )
    if remaining:
        spoken += " Training Wheels remaining: " + ", ".join(remaining) + "."
    return {
        "ok": True,
        "locked": locked,
        "refused": refused,
        "remaining": remaining,
        "spoken": spoken,
    }


async def complete_step(session: AsyncSession, step: str) -> dict:
    from app.ev.assistant import get_profile
    from app.ev.protocols import complete_training_wheels, start_training_wheels

    if step not in TRAINING_STEPS:
        return {"ok": False, "error": f"unknown step '{step}'", "remaining": list(TRAINING_STEPS)}
    profile = await get_profile(session)
    if profile.training_wheels_started_at is None:
        await start_training_wheels(session)
        profile = await get_profile(session)
    steps = _steps_map(profile)
    if not steps.get(step):
        steps[step] = utcnow().isoformat()
    profile.training_steps = steps
    profile.updated_at = utcnow()
    await session.flush()
    remaining = [name for name in TRAINING_STEPS if not steps.get(name)]
    unlocked: list[str] = []
    dedication = None
    completed = False
    if not remaining:
        finished = await complete_training_wheels(session)
        unlocked = list(UNLOCK_ON_COMPLETE) if finished.get("completed") else []
        dedication = finished.get("dedication")
        completed = bool(finished.get("completed"))
    return {
        "ok": True,
        "step": step,
        "remaining": remaining,
        "unlocked": unlocked,
        "dedication": dedication,
        "completed": completed,
    }


async def unlock_after_training(session: AsyncSession) -> list[str]:
    unlocked: list[str] = []
    for key in UNLOCK_ON_COMPLETE:
        await set_gate(session, key, "enabled", reason="training_wheels_complete")
        unlocked.append(key)
    await set_gate(
        session,
        "actuator.drone",
        "locked",
        reason="Hobby drone adapter is not configured (item 45).",
    )
    for key, _title, detail in REFUSED_PROTOCOLS:
        await set_gate(session, key, "refused", reason=detail)
    await session.flush()
    return unlocked


async def refuse_if_locked(session: AsyncSession, spec: dict | None) -> dict | None:
    """Return a refuse payload when the tool maps to a locked/refused gate."""

    if spec is None:
        return None
    permission = str(spec.get("permission") or "")
    read_only = bool(spec.get("read_only"))
    gate_key = PERMISSION_GATES.get(permission)

    if _GATES_UNAVAILABLE:
        if read_only:
            return None
        if permission in ACTUATOR_PERMISSIONS or not read_only and gate_key:
            remaining = list(TRAINING_STEPS)
            return {
                "ok": False,
                "error": "training_wheels",
                "remaining": remaining,
            }
        return None

    if gate_key is None:
        return None

    row = await _gate(session, gate_key)
    if row is None:
        if permission in ACTUATOR_PERMISSIONS:
            return {
                "ok": False,
                "error": "training_wheels",
                "remaining": await remaining_steps(session),
            }
        return None
    if row.status == "refused":
        return {"ok": False, "error": "refused", "remaining": [], "gate": gate_key}
    if row.status == "locked":
        return {
            "ok": False,
            "error": "training_wheels",
            "remaining": await remaining_steps(session),
            "gate": gate_key,
        }
    return None


async def mark_step_from_event(session: AsyncSession, step: str) -> None:
    """Idempotent hook so real owner actions complete the checklist."""

    try:
        await complete_step(session, step)
    except Exception:
        return
