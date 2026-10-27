"""RC SEAM: HANDS skills behind the brain's tool surface (B4/F1).

`skills.act` / `world.query` route through the fabric policy gate to
SKILLS_API. Reads run; effects park for approval; nothing here touches the
real desktop (find/list are pure-registry; run parks before acting).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
    from app.fabric import grants, panic

    grants.revoke_all()
    panic.clear()
    return home


async def _dispatch(db_session: AsyncSession, name: str, args: dict) -> dict[str, Any]:
    from app.ev.tools import dispatch

    response = await dispatch(
        db_session, name, args, actor="master", allow_sensitive=True)
    assert isinstance(response.result, dict)
    return response.result


async def test_tool_surface_lists_skills_and_world() -> None:
    from app.cognitive.capabilities import tool_specs

    names = {spec.name for spec in tool_specs()}
    assert "skills.act" in names
    assert "world.query" in names
    by_name = {spec.name: spec for spec in tool_specs()}
    assert by_name["skills.act"].parameters["required"] == ["op"]
    assert by_name["world.query"].read_only is True


async def test_skills_find_and_list_are_registry_reads(
    db_session: AsyncSession, fabric_on
) -> None:
    found = await _dispatch(db_session, "skills_act",
                            {"op": "find", "intent": "skip the ad"})
    assert found["ok"] is True, found
    assert found["contract"] == "evie-hands/1"
    rows = found["evidence"]["skills"]
    assert rows and rows[0]["name"] == "web_media.skip_ad"
    listed = await _dispatch(db_session, "skills_act", {"op": "list"})
    assert listed["ok"] is True
    assert listed["evidence"]["n"] >= 60
    assert listed["evidence"]["api_version"] == "1.0.0"


async def test_skills_run_parks_for_approval(
    db_session: AsyncSession, fabric_on
) -> None:
    parked = await _dispatch(db_session, "skills_act",
                             {"op": "run", "name": "web_media.skip_ad", "args": {}})
    assert parked["ok"] is False
    assert parked.get("action_id"), parked


async def test_skills_unknown_op_and_missing_name_denied(
    db_session: AsyncSession, fabric_on
) -> None:
    bad = await _dispatch(db_session, "skills_act", {"op": "explode"})
    assert bad["ok"] is False
    nameless = await _dispatch(db_session, "skills_act", {"op": "run"})
    assert nameless["ok"] is False


async def test_skills_disabled_is_honest(
    db_session: AsyncSession, fabric_on, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "hands_enabled", False)
    out = await _dispatch(db_session, "skills_act", {"op": "list"})
    assert out["ok"] is False
    assert out["error"] == "CAPABILITY_UNAVAILABLE"


async def test_world_untrusted_parks(
    db_session: AsyncSession, fabric_on
) -> None:
    parked = await _dispatch(db_session, "world_query",
                             {"op": "query", "select": "tabs.playing",
                              "provenance": "untrusted_content"})
    assert parked["ok"] is False
    assert parked.get("action_id"), parked


async def test_watcher_create_parks_for_approval(
    db_session: AsyncSession, fabric_on
) -> None:
    parked = await _dispatch(db_session, "skills_act",
                             {"op": "watcher", "watcher_op": "create",
                              "spec": {"kind": "ad_skip", "ttl_s": 60}})
    assert parked["ok"] is False
    assert parked.get("action_id"), parked
