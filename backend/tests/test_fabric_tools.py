"""WP1 slice 2: fabric lanes registered as dispatch + semantic tools."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings


@pytest.fixture()
def fabric_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "fabric", True)
    monkeypatch.setattr(settings, "fabric_panic_file", str(tmp_path / "panic.json"))
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    monkeypatch.setattr("app.fabric.roots._home", lambda: home)
    from app.fabric import panic

    panic.clear()
    return home


def _inner(response) -> dict[str, Any]:
    result = response.result
    if hasattr(result, "model_dump"):
        result = result.model_dump()
    assert isinstance(result, dict)
    return result


# --- dispatch: shell ---


async def test_shell_run_auto_echo(db_session: AsyncSession, fabric_on) -> None:
    from app.ev.tools import dispatch

    response = await dispatch(db_session, "shell_run", {"command": "echo hi"}, actor="master", allow_sensitive=True)
    body = _inner(response)
    assert body["ok"] is True
    assert "hi" in body["evidence"]["out"]


async def test_shell_run_deny(db_session: AsyncSession, fabric_on) -> None:
    from app.ev.tools import dispatch

    response = await dispatch(db_session, "shell_run", {"command": "sudo whoami"}, actor="master", allow_sensitive=True)
    body = _inner(response)
    assert body["ok"] is False
    assert "denied" in str(body.get("error", "")).lower()


async def test_shell_run_confirm_parks_and_resumes(
    db_session: AsyncSession, fabric_on, tmp_path: Path
) -> None:
    from app.ev.tools import dispatch
    from app.services import runtime as runtime_service

    target = fabric_on / "Desktop" / "parked.txt"
    assert not target.exists()
    parked = await dispatch(
        db_session, "shell_run", {"command": "touch parked.txt"}, actor="master",
        allow_sensitive=True,
    )
    body = _inner(parked)
    assert body["ok"] is False
    assert body.get("action_id"), body
    assert not target.exists(), "parked effect must not execute"
    await db_session.commit()

    from uuid import UUID

    action = await runtime_service.decide_action(
        db_session, UUID(str(body["action_id"])), actor="master", decision="approve"
    )
    await db_session.commit()
    assert action.status == "executed"
    assert target.exists(), "approved effect must execute exactly once"


async def test_shell_session_start_tail_cancel(db_session: AsyncSession, fabric_on) -> None:
    from app.ev.tools import dispatch

    started = _inner(await dispatch(
        db_session, "shell_session",
        {"op": "start", "cmd": "echo sess-hello"}, actor="master", allow_sensitive=True))
    assert started["ok"] is True
    sid = started["session_id"]
    tail = _inner(await dispatch(
        db_session, "shell_session", {"op": "tail", "session_id": sid, "n": 20},
        actor="master", allow_sensitive=True))
    assert tail["ok"] is True
    cancelled = _inner(await dispatch(
        db_session, "shell_session", {"op": "cancel", "session_id": sid}, actor="master", allow_sensitive=True))
    assert cancelled["ok"] is True


# --- dispatch: terminal layouts ---


async def test_terminal_layout_plan_is_pure(db_session: AsyncSession, fabric_on) -> None:
    from app.ev.tools import dispatch

    body = _inner(await dispatch(
        db_session, "terminal_layout",
        {"op": "plan", "panes": [{"command": "echo a"}, {"command": "echo b"}]},
        actor="master", allow_sensitive=True))
    assert body["ok"] is True
    assert len(body["evidence"]["plan"]) == 2


def test_layout_open_fails_when_splits_do_not_grow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.fabric.terminal import TmuxLayout

    layout = TmuxLayout(root=tmp_path)
    monkeypatch.setattr("app.fabric.terminal.has_tmux", lambda: True)
    monkeypatch.setattr("app.fabric.terminal._pane_ids", lambda name, socket: ["%0"])

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(layout, "_t", lambda *args: _Proc())
    result = layout.open([{"command": "echo a"}, {"command": "echo b"}], confirm=True)
    assert result["ok"] is False
    assert result["verified"] is False
    assert "1 of 2" in result["error"]


def test_layout_store_roundtrip(tmp_path: Path) -> None:
    from app.fabric import terminal

    spec = {"panes": [{"command": "echo a", "cwd": str(tmp_path)}]}
    saved = terminal.save_layout("unit", spec, root=tmp_path)
    assert saved["ok"] is True
    names = [row["name"] for row in terminal.list_layouts(root=tmp_path)["layouts"]]
    assert "unit" in names
    assert terminal.load_layout("unit", root=tmp_path)["spec"]["panes"][0]["command"] == "echo a"
    assert terminal.load_layout("missing", root=tmp_path)["ok"] is False


# --- dispatch: sql ---


async def test_sql_read_and_dml_preview_and_park(
    db_session: AsyncSession, fabric_on, tmp_path: Path
) -> None:
    from app.ev.tools import dispatch
    from app.services import runtime as runtime_service

    db_path = str(tmp_path / "scratch.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE t (a TEXT)")
    conn.execute("INSERT INTO t VALUES ('x')")
    conn.commit()
    conn.close()

    read = _inner(await dispatch(
        db_session, "sql_query", {"sql": "SELECT * FROM t", "db_path": db_path},
        actor="master", allow_sensitive=True))
    assert read["ok"] is True
    assert read["evidence"]["rows"]

    preview = _inner(await dispatch(
        db_session, "sql_query",
        {"sql": "UPDATE t SET a='y'", "db_path": db_path, "preview": True},
        actor="master", allow_sensitive=True))
    assert preview["ok"] is True
    assert preview["evidence"].get("preview") is True
    # Preview rolled back: data unchanged.
    assert sqlite3.connect(db_path).execute("SELECT a FROM t").fetchone()[0] == "x"

    parked = _inner(await dispatch(
        db_session, "sql_query",
        {"sql": "UPDATE t SET a='y'", "db_path": db_path},
        actor="master", allow_sensitive=True))
    assert parked["ok"] is False
    assert parked.get("action_id"), parked
    await db_session.commit()

    from uuid import UUID

    action = await runtime_service.decide_action(
        db_session, UUID(str(parked["action_id"])), actor="master", decision="approve"
    )
    await db_session.commit()
    assert action.status == "executed"
    assert sqlite3.connect(db_path).execute("SELECT a FROM t").fetchone()[0] == "y"


async def test_untrusted_sql_read_is_not_called_done(
    db_session: AsyncSession, fabric_on
) -> None:
    from app.ev.tools import dispatch

    body = _inner(await dispatch(
        db_session, "sql_query",
        {"sql": "SELECT 1", "provenance": "untrusted_content"},
        actor="master", allow_sensitive=True))
    assert body.get("action_id"), body
    assert "Query ran" not in str(body.get("spoken") or "")


async def test_sql_prod_denied(db_session: AsyncSession, fabric_on) -> None:
    from app.ev.tools import dispatch

    body = _inner(await dispatch(
        db_session, "sql_query", {"sql": "SELECT 1", "db_path": "prod"}, actor="master", allow_sensitive=True))
    assert body["ok"] is False


# --- dispatch: screen_ref ---


async def test_screen_ref_resolve_last_none_then_found(
    db_session: AsyncSession, fabric_on, tmp_path: Path
) -> None:
    from app.ev.tools import dispatch

    shot_dir = tmp_path / "shots"
    shot_dir.mkdir()
    none = _inner(await dispatch(
        db_session, "screen_ref", {"op": "resolve_last", "directory": str(shot_dir)},
        actor="master", allow_sensitive=True))
    assert none["ok"] is False
    assert "no screenshot" in str(none.get("error", "")).lower()

    (shot_dir / "Screenshot 2026-10-18 at 1.00.00 AM.png").write_bytes(b"fake-png")
    found = _inner(await dispatch(
        db_session, "screen_ref", {"op": "resolve_last", "directory": str(shot_dir)},
        actor="master", allow_sensitive=True))
    assert found["ok"] is True
    assert found["evidence"]["source_hash"]


async def test_screen_ref_infer_panes(db_session: AsyncSession, fabric_on) -> None:
    from app.ev.tools import dispatch

    ocr = [{"text": "a", "x": 0, "y": 0}, {"text": "b", "x": 0, "y": 200}]
    body = _inner(await dispatch(
        db_session, "screen_ref", {"op": "infer", "ocr": ocr}, actor="master", allow_sensitive=True))
    assert body["ok"] is True
    assert len(body["evidence"]["panes"]) == 2


# --- dispatch: pim (file-backed lane contract; EventKit in WP8) ---


async def test_pim_reminder_lifecycle(db_session: AsyncSession, fabric_on) -> None:
    from app.ev.tools import dispatch

    created = _inner(await dispatch(
        db_session, "pim_act",
        {"op": "reminder_create", "title": "buy milk", "list_name": "unit",
         "backend": "file"},
        actor="master", allow_sensitive=True))
    assert created["ok"] is True
    rid = created["evidence"]["id"]
    listed = _inner(await dispatch(
        db_session, "pim_act", {"op": "reminder_list", "list_name": "unit",
                               "backend": "file"},
        actor="master", allow_sensitive=True))
    assert rid in [r["id"] for r in listed["evidence"]["reminders"]]
    done = _inner(await dispatch(
        db_session, "pim_act", {"op": "reminder_complete", "id": rid,
                               "backend": "file"},
        actor="master", allow_sensitive=True))
    assert done["ok"] is True
    deleted = _inner(await dispatch(
        db_session, "pim_act", {"op": "reminder_delete", "id": rid, "backend": "file"},
        actor="master", allow_sensitive=True))
    assert deleted["ok"] is True


async def test_pim_reminder_keeps_its_due_time(
    db_session: AsyncSession, fabric_on
) -> None:
    from app.ev.tools import dispatch

    due = "2026-10-20T09:00:00Z"
    created = _inner(await dispatch(
        db_session, "pim_act",
        {"op": "reminder_create", "title": "stand up", "backend": "file", "due": due},
        actor="master", allow_sensitive=True))
    assert created["ok"] is True and created["verified"] is True, created
    assert created["evidence"]["due"] == due
    listed = _inner(await dispatch(
        db_session, "pim_act",
        {"op": "reminder_list", "backend": "file"},
        actor="master", allow_sensitive=True))
    row = next(item for item in listed["evidence"]["reminders"]
               if item["id"] == created["evidence"]["id"])
    assert row["due"] == due


async def test_pim_eventkit_create_forwards_due(
    db_session: AsyncSession, fabric_on, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.ev.tools import dispatch

    seen: dict[str, str] = {}

    def fake(self, title: str, list_name: str = "", notes: str = "", due: str = ""):
        del self, list_name, notes
        seen["title"] = title
        seen["due"] = due
        return {"ok": True, "verified": True,
                "evidence": {"id": "ek-1", "due": due}, "error": None, "undo": None}

    monkeypatch.setattr("app.fabric.bridge.Bridge.ek_reminder_create", fake)
    due = "2026-10-20T09:00:00Z"
    created = _inner(await dispatch(
        db_session, "pim_act",
        {"op": "reminder_create", "title": "stand up", "backend": "eventkit", "due": due},
        actor="master", allow_sensitive=True))
    assert created["ok"] is True, created
    assert seen == {"title": "stand up", "due": due}


async def test_pim_note_lifecycle(db_session: AsyncSession, fabric_on) -> None:
    from app.ev.tools import dispatch

    created = _inner(await dispatch(
        db_session, "pim_act",
        {"op": "note_create", "title": "grocery", "body": "milk",
         "folder": "unit", "backend": "file"},
        actor="master", allow_sensitive=True))
    assert created["ok"] is True and created["verified"] is True
    assert created["spoken"] == "Done."
    nid = created["evidence"]["id"]
    listed = _inner(await dispatch(
        db_session, "pim_act",
        {"op": "note_list", "folder": "unit", "backend": "file"},
        actor="master", allow_sensitive=True))
    assert nid in [row["id"] for row in listed["evidence"]["notes"]]
    assert any(row["body"] == "milk" for row in listed["evidence"]["notes"])
    deleted = _inner(await dispatch(
        db_session, "pim_act",
        {"op": "note_delete", "id": nid, "backend": "file"},
        actor="master", allow_sensitive=True))
    assert deleted["ok"] is True and deleted["verified"] is True
    listed = _inner(await dispatch(
        db_session, "pim_act",
        {"op": "note_list", "folder": "unit", "backend": "file"},
        actor="master", allow_sensitive=True))
    assert nid not in [row["id"] for row in listed["evidence"]["notes"]]


async def test_calendar_list_reads_calendars_not_events(
    db_session: AsyncSession, fabric_on, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.ev.tools import dispatch
    from app.fabric.bridge import Bridge

    calls: list[str] = []

    def calendars(self):
        calls.append("list")
        return {"ok": True, "verified": True,
                "evidence": {"calendars": ["Home"], "n": 1},
                "error": None, "undo": None}

    def events(self, calendar="", days=7):
        calls.append(f"events:{calendar}:{days}")
        return {"ok": True, "verified": True, "evidence": {"events": [], "n": 0},
                "error": None, "undo": None}

    monkeypatch.setattr(Bridge, "run_via_app", lambda self, *args, **kwargs: {"ok": True})
    monkeypatch.setattr(Bridge, "ek_calendars", calendars)
    monkeypatch.setattr(Bridge, "ek_calendar_events", events)
    listed = _inner(await dispatch(
        db_session, "pim_act", {"op": "calendar_list", "backend": "auto"},
        actor="master", allow_sensitive=True))
    events_body = _inner(await dispatch(
        db_session, "pim_act",
        {"op": "calendar_events", "list_name": "Home", "days": 3, "backend": "auto"},
        actor="master", allow_sensitive=True))
    assert listed["spoken"] == "1 calendars."
    assert events_body["spoken"] == "0 events."
    assert calls == ["list", "events:Home:3"]


# --- dispatch: builder ---


async def test_builder_plan_auto_apply_parks(
    db_session: AsyncSession, fabric_on, tmp_path: Path
) -> None:
    from app.ev.tools import dispatch

    plan = _inner(await dispatch(
        db_session, "builder_job",
        {"op": "plan", "goal": "unit job",
         "steps": [{"name": "say", "cmd": "echo built"}]},
        actor="master", allow_sensitive=True))
    assert plan["ok"] is True
    assert plan["job_id"]

    parked = _inner(await dispatch(
        db_session, "builder_job",
        {"op": "apply", "plan": plan["plan"], "job_id": plan["job_id"]},
        actor="master", allow_sensitive=True))
    assert parked["ok"] is False
    assert parked.get("action_id"), parked


# --- dispatch: atlas ---


async def test_atlas_discover_no_phantom(db_session: AsyncSession, fabric_on) -> None:
    from app.ev.tools import dispatch

    body = _inner(await dispatch(
        db_session, "atlas_discover", {"domain": "notes"}, actor="master", allow_sensitive=True))
    assert body["ok"] is True
    assert isinstance(body["evidence"].get("descriptors"), list)


# --- dispatch: gates ---


async def test_fabric_tools_fail_closed_when_disabled(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.ev.tools import dispatch

    monkeypatch.setattr(settings, "fabric", False)
    body = _inner(await dispatch(
        db_session, "shell_run", {"command": "echo hi"}, actor="master", allow_sensitive=True))
    assert body["ok"] is False
    assert body.get("error") == "CAPABILITY_UNAVAILABLE"


async def test_panic_denies_effects_but_allows_reads(
    db_session: AsyncSession, fabric_on
) -> None:
    from app.ev.tools import dispatch
    from app.fabric import panic

    panic.panic("unit test")
    denied = _inner(await dispatch(
        db_session, "shell_run", {"command": "touch nope.txt"}, actor="master", allow_sensitive=True))
    assert denied["ok"] is False
    assert "panic" in str(denied.get("error", "")).lower()
    assert denied.get("action_id") is None, "panic denies outright, never parks"
    allowed = _inner(await dispatch(
        db_session, "shell_run", {"command": "echo still-here"}, actor="master", allow_sensitive=True))
    assert allowed["ok"] is True
    panic.clear()


async def test_prepare_only_short_circuits(
    db_session: AsyncSession, fabric_on
) -> None:
    from app.ev.tools import dispatch

    body = _inner(await dispatch(
        db_session, "shell_run", {"command": "touch po.txt", "prepare_only": True},
        actor="master", allow_sensitive=True))
    assert body["ok"] is True
    assert body.get("prepare_only") is True
    assert not (fabric_on / "Desktop" / "po.txt").exists()


# --- semantic routing: each new tool reaches its dispatch backend ---


def _install_dispatch(monkeypatch: pytest.MonkeyPatch, result: dict | None = None):
    seen: dict[str, Any] = {}

    async def fake_dispatch(session, name, arguments, **kwargs):
        seen["name"] = name
        seen["args"] = dict(arguments or {})
        return dict(result or {"ok": True, "spoken": "done."})

    monkeypatch.setattr("app.ev.tools.dispatch", fake_dispatch)
    return seen


async def _run_semantic(name: str, arguments: dict[str, Any]):
    from app.cognitive.executor import execute_semantic
    from app.cognitive.session_store import CognitiveSession, reset_for_tests

    reset_for_tests()
    try:
        return await execute_semantic(
            None, name, arguments, cognition=CognitiveSession(session_id="fab"),
            actor="master", live_session_id=None, steering_seen=0,
        )
    finally:
        reset_for_tests()


@pytest.mark.parametrize(
    ("semantic", "dispatched", "args"),
    [
        ("shell.run", "shell_run", {"command": "echo hi"}),
        ("shell.session", "shell_session", {"op": "list"}),
        ("terminal.layout", "terminal_layout", {"op": "list"}),
        ("screen.ref", "screen_ref", {"op": "resolve_last"}),
        ("sql.query", "sql_query", {"sql": "SELECT 1"}),
        ("pim.act", "pim_act", {"op": "reminder_list"}),
        ("builder.job", "builder_job", {"op": "plan", "goal": "g"}),
        ("atlas.discover", "atlas_discover", {"domain": "notes"}),
    ],
)
async def test_semantic_routes_to_dispatch(
    monkeypatch: pytest.MonkeyPatch, semantic: str, dispatched: str, args: dict
) -> None:
    seen = _install_dispatch(monkeypatch)
    out = await _run_semantic(semantic, args)
    assert seen["name"] == dispatched
    for key, value in args.items():
        assert seen["args"][key] == value
    assert out["ok"] is True


def test_capability_discover_lists_fabric_tools() -> None:
    from app.cognitive.capabilities import public_descriptors

    names = {row["name"] for row in public_descriptors()}
    for expected in ("shell.run", "shell.session", "terminal.layout", "screen.ref",
                     "sql.query", "pim.act", "builder.job", "atlas.discover"):
        assert expected in names


async def test_files_act_explicit_op_routes_to_files_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _install_dispatch(monkeypatch)
    out = await _run_semantic(
        "files.act", {"op": "create", "path": "x.txt", "content": "x"}
    )
    assert seen["name"] == "files_op"
    assert seen["args"]["op"] == "create"
    assert seen["args"]["path"] == "x.txt"
    assert out["ok"] is True


async def test_files_act_effect_keeps_legacy_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _install_dispatch(monkeypatch)
    out = await _run_semantic("files.act", {"effect": "save hello to x.txt"})
    assert seen["name"] == "computer"
    assert out["ok"] is True


async def test_files_act_write_alias_maps_to_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _install_dispatch(monkeypatch)
    await _run_semantic("files.act", {"op": "write", "path": "x.txt"})
    assert seen["name"] == "files_op"
    assert seen["args"]["op"] == "create"


async def test_files_act_explicit_op_end_to_end_live_lane(
    db_session, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S1-S6 live path: kernel files.act with explicit op reaches the real lane."""
    from pathlib import Path

    from app.cognitive.executor import execute_semantic
    from app.cognitive.session_store import CognitiveSession, reset_for_tests

    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    monkeypatch.setattr("app.fabric.roots._home", lambda: home)
    monkeypatch.setattr(settings, "fabric", True)
    monkeypatch.setattr(settings, "fabric_panic_file", str(tmp_path / "panic.json"))
    reset_for_tests()
    try:
        out = await execute_semantic(
            db_session, "files.act",
            {"op": "create", "path": "live.txt", "content": "via-kernel"},
            cognition=CognitiveSession(session_id="files-e2e"),
            actor="master", live_session_id=None, steering_seen=0,
        )
    finally:
        reset_for_tests()
    assert out["ok"] is True, out
    assert Path(home / "Desktop" / "live.txt").read_text() == "via-kernel"
