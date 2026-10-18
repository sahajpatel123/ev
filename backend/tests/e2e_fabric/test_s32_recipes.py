"""S32: recipes save/parameterize/replay under the same policy."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings


@pytest.fixture()
def fabric_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "fabric", True)
    monkeypatch.setattr(settings, "fabric_panic_file", str(tmp_path / "panic.json"))
    monkeypatch.setattr(settings, "fabric_audit_file", str(tmp_path / "audit.jsonl"))
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    monkeypatch.setattr("app.fabric.roots._home", lambda: home)
    from app.fabric import panic

    panic.clear()
    return home


async def _dispatch(db_session: AsyncSession, name: str, args: dict) -> dict[str, Any]:
    from app.ev.tools import dispatch

    response = await dispatch(
        db_session, name, args, actor="master", allow_sensitive=True)
    assert isinstance(response.result, dict)
    return response.result


def _steps():
    return [
        {"tool": "files_op",
         "args": {"op": "create", "path": "{name}.txt", "content": "hello {who}"}},
        {"tool": "files_op",
         "args": {"op": "read", "path": "{name}.txt"}},
    ]


async def test_s32_save_and_parameterized_replay(
    db_session: AsyncSession, fabric_on
) -> None:
    saved = await _dispatch(db_session, "recipe_act",
                            {"op": "save", "name": "greet",
                             "steps": _steps()})
    assert saved["ok"], saved
    ran = await _dispatch(db_session, "recipe_act",
                          {"op": "run", "name": "greet",
                           "params": {"name": "ada", "who": "world"}})
    assert ran["ok"], ran
    assert ran["evidence"]["n"] == 2
    assert (fabric_on / "Desktop" / "ada.txt").read_text() == "hello world"
    # Second replay with different params reuses the same recipe.
    ran2 = await _dispatch(db_session, "recipe_act",
                           {"op": "run", "name": "greet",
                            "params": {"name": "bob", "who": "there"}})
    assert ran2["ok"], ran2
    assert (fabric_on / "Desktop" / "bob.txt").read_text() == "hello there"


async def test_s32_replay_enforces_policy_and_resumes(
    db_session: AsyncSession, fabric_on
) -> None:
    from app.services import runtime as runtime_service

    saved = await _dispatch(db_session, "recipe_act",
                            {"op": "save", "name": "touchy",
                             "steps": [{"tool": "shell_run",
                                        "args": {"command": "touch {name}.txt"}}]})
    assert saved["ok"], saved
    parked = await _dispatch(db_session, "recipe_act",
                             {"op": "run", "name": "touchy",
                              "params": {"name": "held"}})
    assert parked["ok"] is False
    assert parked["evidence"].get("parked") is True
    assert parked["evidence"].get("action_id"), parked
    assert not (fabric_on / "Desktop" / "held.txt").exists()
    await db_session.commit()
    action = await runtime_service.decide_action(
        db_session, UUID(str(parked["evidence"]["action_id"])),
        actor="master", decision="approve")
    await db_session.commit()
    assert action.status == "executed"
    assert (fabric_on / "Desktop" / "held.txt").exists()


async def test_s32_missing_param_fails_closed(db_session: AsyncSession, fabric_on) -> None:
    await _dispatch(db_session, "recipe_act",
                    {"op": "save", "name": "needful", "steps": _steps()})
    out = await _dispatch(db_session, "recipe_act",
                          {"op": "run", "name": "needful",
                           "params": {"name": "x"}})
    assert out["ok"] is False
    assert out["evidence"]["missing_params"] == ["who"]


async def test_s32_unknown_tool_rejected_at_save(
    db_session: AsyncSession, fabric_on
) -> None:
    out = await _dispatch(db_session, "recipe_act",
                          {"op": "save", "name": "bad",
                           "steps": [{"tool": "nope_not_a_tool", "args": {}}]})
    assert out["ok"] is False
    assert "unknown tool" in out["error"]


async def test_s32_list_get_delete_roundtrip(db_session: AsyncSession, fabric_on) -> None:
    await _dispatch(db_session, "recipe_act",
                    {"op": "save", "name": "temp", "steps": _steps()})
    listed = await _dispatch(db_session, "recipe_act", {"op": "list"})
    assert "temp" in listed["evidence"]["recipes"]
    got = await _dispatch(db_session, "recipe_act", {"op": "get", "name": "temp"})
    assert len(got["evidence"]["steps"]) == 2
    deleted = await _dispatch(db_session, "recipe_act",
                              {"op": "delete", "name": "temp"})
    assert deleted["ok"] is True
    listed2 = await _dispatch(db_session, "recipe_act", {"op": "list"})
    assert "temp" not in listed2["evidence"]["recipes"]


async def test_s32_scheduler_never_fires_recipes(
    db_session: AsyncSession, fabric_on
) -> None:
    """Recipe rows are inert to the scheduler (empty trigger, manual runs)."""
    from app.models import Routine

    await _dispatch(db_session, "recipe_act",
                    {"op": "save", "name": "inert", "steps": _steps()})
    from sqlalchemy import select

    rows = (await db_session.execute(
        select(Routine).where(Routine.name == "inert"))).scalars().all()
    assert len(rows) == 1
    assert rows[0].kind == "trigger"
    assert rows[0].trigger.get("event_type") == "fabric.recipe.manual"
    assert rows[0].next_run_at is None
