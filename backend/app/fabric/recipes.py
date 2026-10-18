"""Parameterized recipes on the routines service (WP6).

A recipe is a Routine row (kind=trigger, empty trigger so the scheduler
never fires it, metadata.recipe=True) whose payload is an ordered step
list of {tool, args} with {param} placeholders. `run` substitutes params
and re-dispatches every step, so replay enforces the same policy as the
original turn; a parked step pauses the run with its ticket, resumable
via start_from after approval.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

RECIPE_ACTION = "fabric_recipe"
RECIPE_TRIGGER = "fabric.recipe.manual"
_PARAM_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _placeholders(value: Any, acc: set[str]) -> None:
    if isinstance(value, str):
        acc.update(_PARAM_RE.findall(value))
    elif isinstance(value, dict):
        for item in value.values():
            _placeholders(item, acc)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _placeholders(item, acc)


def _substitute(value: Any, params: dict[str, Any]) -> Any:
    if isinstance(value, str):
        def fill(match: re.Match) -> str:
            return str(params.get(match.group(1), match.group(0)))

        return _PARAM_RE.sub(fill, value)
    if isinstance(value, dict):
        return {key: _substitute(item, params) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute(item, params) for item in value]
    return value


def _receipt(ok: bool, evidence=None, error=None) -> dict[str, Any]:
    return {"ok": ok, "verified": True, "evidence": evidence or {},
            "error": error, "undo": None}


async def save(session: AsyncSession, name: str, steps: list[dict],
               *, params: dict | None = None, title: str | None = None,
               actor: str = "master") -> dict[str, Any]:
    from app.ev.tools import get_spec
    from app.routines.service import create_routine
    from app.schemas import RoutineCreate

    name = (name or "").strip()
    if not name or len(name) > 128 or "/" in name:
        return _receipt(False, error="bad recipe name")
    if not isinstance(steps, list) or not steps or len(steps) > 24:
        return _receipt(False, error="steps must be 1-24 ops")
    for i, step in enumerate(steps):
        if not isinstance(step, dict) or not isinstance(step.get("tool"), str):
            return _receipt(False, error=f"step {i} needs a tool name")
        if get_spec(step["tool"]) is None:
            return _receipt(False, error=f"step {i}: unknown tool '{step['tool']}'")
        if not isinstance(step.get("args", {}), dict):
            return _receipt(False, error=f"step {i}: args must be an object")
    routine = await create_routine(
        session,
        RoutineCreate(name=name, kind="trigger",
                      # Manual-only channel: nothing emits this event; run()
                      # is the sole executor, so the scheduler never fires it.
                      trigger={"event_type": RECIPE_TRIGGER},
                      action_type=RECIPE_ACTION,
                      action_title=title or f"recipe: {name}",
                      action_payload={"steps": steps},
                      metadata={"recipe": True, "params": dict(params or {})}),
        actor=actor,
    )
    await session.commit()
    return _receipt(True, {"recipe_id": str(routine.id), "name": name,
                           "steps": len(steps)})


async def _find(session: AsyncSession, name_or_id: str):
    from app.models import Routine

    key = (name_or_id or "").strip()
    row = None
    try:
        row = await session.get(Routine, UUID(key))
    except (ValueError, TypeError, AttributeError):
        row = None
    if row is None:
        rows = (await session.execute(
            select(Routine).where(Routine.name == key))).scalars().all()
        row = rows[0] if rows else None
    if row is None or not isinstance(row.metadata_, dict) or not row.metadata_.get("recipe"):
        return None
    return row


async def get_recipe(session: AsyncSession, name_or_id: str) -> dict[str, Any]:
    row = await _find(session, name_or_id)
    if row is None:
        return _receipt(False, error="unknown recipe")
    return _receipt(True, {"recipe_id": str(row.id), "name": row.name,
                           "steps": (row.action_payload or {}).get("steps", []),
                           "params": (row.metadata_ or {}).get("params", {})})


async def list_recipes(session: AsyncSession) -> dict[str, Any]:
    from app.models import Routine

    rows = (await session.execute(select(Routine))).scalars().all()
    names = sorted(row.name for row in rows
                   if isinstance(row.metadata_, dict) and row.metadata_.get("recipe"))
    return _receipt(True, {"recipes": names, "n": len(names)})


async def delete_recipe(session: AsyncSession, name_or_id: str) -> dict[str, Any]:
    row = await _find(session, name_or_id)
    if row is None:
        return _receipt(False, error="unknown recipe")
    name = row.name
    await session.delete(row)
    await session.commit()
    return _receipt(True, {"deleted": name})


async def run(session: AsyncSession, name_or_id: str, params: dict | None = None, *,
              actor: str = "master", device_id=None, live_session_id: str | None = None,
              start_from: int = 0) -> dict[str, Any]:
    from app.ev.tools import dispatch

    row = await _find(session, name_or_id)
    if row is None:
        return _receipt(False, error="unknown recipe")
    steps = (row.action_payload or {}).get("steps", [])
    if not isinstance(steps, list) or not steps:
        return _receipt(False, error="recipe has no steps")
    params = dict(params or {})
    needed: set[str] = set()
    for step in steps:
        _placeholders(step, needed)
    missing = sorted(name for name in needed if name not in params)
    if missing:
        return _receipt(False, {"missing_params": missing},
                        error=f"missing params: {', '.join(missing)}")
    receipts: list[dict] = []
    start = max(0, int(start_from or 0))
    for index in range(start, len(steps)):
        step = steps[index]
        args = _substitute(dict(step.get("args") or {}), params)
        try:
            response = await dispatch(
                session, str(step.get("tool") or ""), args, actor=actor,
                allow_sensitive=True, device_id=device_id,
                live_session_id=live_session_id,
                audit_endpoint="POST /v1/fabric/recipe-run",
            )
        except Exception as exc:
            return _receipt(False, {"step": index, "receipts": receipts},
                            error=f"step {index} crashed: {type(exc).__name__}")
        body = response.result if isinstance(response.result, dict) else {}
        receipts.append({"step": index, "ok": bool(body.get("ok")),
                         "error": body.get("error")})
        if body.get("action_id"):
            await session.commit()
            return {"ok": False, "verified": True,
                    "evidence": {"parked": True, "step": index,
                                 "action_id": body["action_id"],
                                 "receipts": receipts, "resume_from": index},
                    "error": body.get("error") or "confirmation_required",
                    "undo": None}
        if not body.get("ok"):
            await session.commit()
            return _receipt(False, {"step": index, "receipts": receipts},
                            error=str(body.get("error") or f"step {index} failed"))
    await session.commit()
    return _receipt(True, {"steps": receipts, "n": len(receipts)})
