"""Daily-life wires: twin rewind, morning brief, isolation, quiet hours, intern."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.edith import looks_like_twin_query, spoken_twin
from app.ev.home import parse_home_act
from app.ev.luna_code import (
    consume_code_intern_receipt,
    drain_pending_code_jobs,
    enqueue_code_intern,
    intern_ack_spoken,
    last_code_job,
    looks_like_deferred_code,
    maybe_enqueue_code_intern,
    peek_code_intern_receipt,
    remember_code_job,
    shared_code_job,
    spawn_pending_code_intern,
)
from app.ev.tool_select import LIVE_VOICE_TOOLS, resolve_live_action, select_tool
from app.ev.workbench import handle_brief_me
from app.services import runtime as runtime_mod


def test_quiet_hours_and_morning_are_live() -> None:
    assert "set_quiet_hours" in LIVE_VOICE_TOOLS
    assert "brief_me" in LIVE_VOICE_TOOLS
    assert "home_status" in LIVE_VOICE_TOOLS
    assert "present" in LIVE_VOICE_TOOLS
    assert "execute_command" not in LIVE_VOICE_TOOLS
    quiet = resolve_live_action("go quiet until 8")
    assert quiet is not None
    assert quiet[0] == "set_quiet_hours"
    assert quiet[1].get("until") == "8"
    morning = resolve_live_action("morning brief")
    assert morning is not None
    assert morning[0] == "brief_me"
    house = resolve_live_action("how's the house")
    assert house is not None
    assert house[0] == "home_status"
    isolate = resolve_live_action("have I been too isolated")
    assert isolate is not None
    assert isolate[0] == "brief_me"
    assert isolate[1].get("topic") == "isolation"


def test_twin_query_is_not_a_person_card() -> None:
    assert looks_like_twin_query("who was I last October")
    assert looks_like_twin_query("what did I believe")
    assert not looks_like_twin_query("who is Maya")
    assert select_tool("who was I last October").selected == "search_memory"


def test_parse_home_act_from_speech() -> None:
    parsed = parse_home_act("turn on the lab lights")
    assert parsed == {"entity": "lab lights", "action": "on"}
    assert parse_home_act("turn the living room lights off") == {
        "entity": "living room lights",
        "action": "off",
    }
    assert parse_home_act("close the garage") == {"entity": "garage", "action": "close"}
    assert parse_home_act("lock the front door") == {"entity": "front door", "action": "lock"}
    assert parse_home_act("how's the house") is None
    assert parse_home_act("turn on the computer") is None
    assert parse_home_act("open Safari") is None
    garage = resolve_live_action("close the garage")
    assert garage is not None
    assert garage[0] == "home_act"
    assert garage[1] == {"entity": "garage", "action": "close"}


def test_hud_present_is_explicit_not_lost_object() -> None:
    hud = resolve_live_action("put that on the HUD")
    assert hud is not None
    assert hud[0] == "present"
    assert hud[1].get("title")
    assert hud[1].get("body")
    screen = resolve_live_action("Show that on my screen")
    assert screen is not None
    assert screen[0] == "present"
    locate = resolve_live_action("where did I leave my charger")
    assert locate is not None
    assert locate[0] == "search_memory"


def test_deferred_code_is_not_breakfast() -> None:
    assert looks_like_deferred_code("keep going on this overnight")
    assert looks_like_deferred_code("work on that while I sleep")
    assert looks_like_deferred_code("write a python script that prints hi overnight")
    assert not looks_like_deferred_code("overnight oats please")


def test_overnight_write_enqueues_instead_of_running_now(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings
    from app.ev import luna_code as luna

    monkeypatch.setattr(settings, "memory_dir", str(tmp_path / "mem"))
    luna._LAST_CODE_JOBS.clear()
    ack = maybe_enqueue_code_intern("write a python script that prints hi overnight")
    assert ack
    assert "background" in ack.lower()
    assert luna.has_pending_code_intern()


def test_one_body_last_job_is_shared_not_leaked(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings
    from app.ev import luna_code as luna

    monkeypatch.setattr(settings, "memory_dir", str(tmp_path / "mem"))
    remember_code_job(
        {
            "ok": True,
            "workspace": str(tmp_path / "mac-job"),
            "project": "mac-job",
            "files_changed": ["greet.py"],
            "spoken": "I saved greet.py",
            "goal": "gender script",
            "runs": [],
        },
        session_key="mac-ws",
    )
    assert last_code_job("phone-ws") is None
    shared = shared_code_job("phone-ws")
    assert shared is not None
    assert shared["files"] == ["greet.py"]
    luna._LAST_CODE_JOBS.clear()
    revived = last_code_job()
    assert revived is not None
    assert revived["files"] == ["greet.py"]
    assert last_code_job("phone-ws") is None


def test_runtime_tick_spawns_intern_without_awaiting_drain() -> None:
    source = inspect.getsource(runtime_mod.daemon_tick)
    assert "spawn_pending_code_intern" in source
    assert "await drain_pending_code_jobs" not in source


@pytest.mark.asyncio
async def test_intern_spawn_does_not_block(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings
    from app.ev import luna_code as luna

    monkeypatch.setattr(settings, "memory_dir", str(tmp_path / "mem"))
    started = asyncio.Event()
    released = asyncio.Event()

    async def fake_drain():
        started.set()
        await released.wait()
        return {"ok": True}

    monkeypatch.setattr(luna, "drain_pending_code_jobs", fake_drain)
    enqueue_code_intern("run it", session_key="owner")
    assert spawn_pending_code_intern() is True
    assert spawn_pending_code_intern() is False
    await asyncio.wait_for(started.wait(), 0.5)
    released.set()
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_intern_runs_queued_work(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings
    from app.ev.code_runtime import set_active_project, write_file as jail_write

    monkeypatch.setattr(settings, "code_workspace", str(tmp_path))
    monkeypatch.setattr(settings, "code_projects_root", "")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "memory_dir", str(tmp_path / "mem"))
    token = set_active_project(tmp_path)
    try:
        jail_write("hello.py", "print('hello intern')\n")
    finally:
        from app.ev.code_runtime import reset_active_project

        reset_active_project(token)
    remember_code_job(
        {
            "ok": True,
            "workspace": str(tmp_path),
            "project": tmp_path.name,
            "files_changed": ["hello.py"],
            "spoken": "Wrote hello.py",
            "goal": "print hello intern",
            "runs": [],
        },
        session_key="owner",
    )
    enqueue_code_intern("run it", session_key="owner")
    assert intern_ack_spoken()
    result = await drain_pending_code_jobs()
    assert result is not None
    assert result.get("ok") is True
    assert any(item.get("exit_code") == 0 for item in result.get("runs") or [])
    receipt = peek_code_intern_receipt()
    assert receipt
    assert "overnight" in receipt.lower()
    spoken = consume_code_intern_receipt()
    assert spoken == receipt
    assert consume_code_intern_receipt() is None


@pytest.mark.asyncio
async def test_twin_and_isolation_speak_honestly(db_session: AsyncSession) -> None:
    twin = await spoken_twin(db_session, "who was I last October")
    assert twin
    assert "twin" in twin.lower() or "rewind" in twin.lower() or "conversation" in twin.lower()
    brief = await handle_brief_me(db_session, "isolation")
    spoken = str(brief.get("spoken") or "").lower()
    assert brief.get("ok") is True
    assert "substitute" in spoken or "isolated" in spoken or "people" in spoken
