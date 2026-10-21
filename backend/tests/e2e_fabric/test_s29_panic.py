"""S29: `ev panic` halts everything within 2s and denies effects until
the owner re-enables. Plus the tamper-evident audit hash chain."""

from __future__ import annotations

import time
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
    from app.fabric import cancel as cancel_mod
    from app.fabric import panic

    panic.clear()
    cancel_mod.reset()
    return home


async def _dispatch(db_session: AsyncSession, name: str, args: dict) -> dict[str, Any]:
    from app.ev.tools import dispatch

    response = await dispatch(
        db_session, name, args, actor="master", allow_sensitive=True
    )
    assert isinstance(response.result, dict)
    return response.result


MUTATING = [
    ("files_op", {"op": "create", "path": "p.txt", "content": "x"}),
    ("shell_run", {"command": "touch p.txt"}),
    ("shell_session", {"op": "start", "cmd": "echo hi"}),
    ("terminal_layout", {"op": "open", "panes": [{"command": "echo hi"}]}),
    ("sql_query", {"sql": "CREATE TABLE t (a TEXT)"}),
    ("pim_act", {"op": "reminder_create", "title": "t"}),
    ("pim_act", {"op": "note_create", "title": "t", "body": "b", "backend": "file"}),
    ("pim_act", {"op": "note_delete", "id": "note-missing", "backend": "file"}),
    ("builder_job", {"op": "plan", "goal": "g"}),
    ("builder_job", {"op": "apply", "plan": {"steps": []}}),
]
READS = [
    ("files_op", {"op": "list"}),
    ("shell_run", {"command": "echo hi"}),
    ("shell_session", {"op": "list"}),
    ("terminal_layout", {"op": "list"}),
    ("sql_query", {"sql": "SELECT 1"}),
    ("screen_ref", {"op": "infer", "ocr": []}),
    ("pim_act", {"op": "reminder_list"}),
    ("atlas_discover", {"domain": "notes"}),
]


async def test_s29_panic_denies_every_effect_lane(
    db_session: AsyncSession, fabric_on
) -> None:
    from app.fabric import panic

    panic.panic("s29")
    for name, args in MUTATING:
        body = await _dispatch(db_session, name, args)
        assert body["ok"] is False, (name, args)
        assert "panic" in str(body.get("error", "")).lower(), (name, body)
        assert body.get("action_id") is None, (name, body)
    panic.clear()


async def test_s29_panic_allows_reads(db_session: AsyncSession, fabric_on) -> None:
    from app.fabric import panic

    panic.panic("s29")
    for name, args in READS:
        body = await _dispatch(db_session, name, args)
        assert body["ok"] is True, (name, body)
    panic.clear()


async def test_s29_panic_kills_live_sessions(
    db_session: AsyncSession, fabric_on
) -> None:
    import subprocess

    from app.fabric import panic
    from app.fabric.shell_sessions import ShellSessions

    root = fabric_on / "Desktop"
    ids = []
    for _ in range(2):
        started = ShellSessions(root=root).start("sleep 60", confirm=True)
        assert started["ok"] is True
        ids.append(started["session_id"])
    t0 = time.time()
    receipt = panic.panic("s29")
    assert time.time() - t0 < 2.0
    assert receipt["ok"] is True
    assert receipt["evidence"]["stopped_sessions"] >= 2
    for sid in ids:
        name = f"ev-{sid}"
        probe = subprocess.run(["tmux", "has-session", "-t", name],
                               capture_output=True, timeout=10)
        assert probe.returncode != 0, f"session {name} still alive after panic"
    panic.clear()


async def test_s29_panic_cancels_registered_turns(fabric_on) -> None:
    from app.fabric import cancel as cancel_mod
    from app.fabric import panic

    token = cancel_mod.new_token("ft-doomed")
    panic.panic("s29")
    assert token.cancelled is True
    panic.clear()


async def test_s29_clear_reenables(db_session: AsyncSession, fabric_on) -> None:
    from app.fabric import panic

    panic.panic("s29")
    denied = await _dispatch(db_session, "shell_run", {"command": "touch x.txt"})
    assert denied["ok"] is False
    panic.clear()
    parked = await _dispatch(db_session, "shell_run", {"command": "touch x.txt"})
    assert parked.get("action_id"), parked


async def test_s29_cli_panic_posts_endpoint() -> None:
    from clients.cli import panic as cli_panic
    from clients.cli import panic_clear, panic_status

    seen: dict[str, Any] = {}

    class FakeResp:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class FakeClient:
        async def post(self, path: str, json: dict | None = None):
            seen["post"] = (path, json)
            return FakeResp({"ok": True})

        async def get(self, path: str):
            seen["get"] = path
            return FakeResp({"ok": True, "panicked": False})

    await cli_panic("unit reason", client=FakeClient())
    assert seen["post"][0] == "/v1/fabric/panic"
    assert seen["post"][1]["reason"] == "unit reason"
    await panic_clear(client=FakeClient())
    assert seen["post"][0] == "/v1/fabric/panic/clear"
    out = await panic_status(client=FakeClient())
    assert seen["get"] == "/v1/fabric/panic"
    assert out["panicked"] is False


def test_s29_cli_parser_has_panic() -> None:
    from clients.cli import build_parser

    args = build_parser().parse_args(["panic"])
    assert args.command == "panic"
    args = build_parser().parse_args(["panic", "--clear"])
    assert args.clear is True
    args = build_parser().parse_args(["panic", "--status"])
    assert args.status is True


# --- audit hash chain ---


def test_s29_audit_chain_verifies(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "fabric_audit_file", str(tmp_path / "a.jsonl"))
    from app.fabric import audit

    audit.record("shell", "run", "echo hi", "AUTO", True)
    audit.record("files", "create", "a.txt", "AUTO_JOURNAL", True)
    audit.record("shell", "run", "sudo x", "DENY", False)
    result = audit.verify()
    assert result["ok"] is True
    assert result["checked"] == 3


def test_s29_audit_tamper_detected(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "fabric_audit_file", str(tmp_path / "a.jsonl"))
    from app.fabric import audit

    audit.record("shell", "run", "echo hi", "AUTO", True)
    audit.record("shell", "run", "echo yo", "AUTO", True)
    path = tmp_path / "a.jsonl"
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    # Flip the decision of the first entry: chain must break.
    lines[0] = lines[0].replace("AUTO", "DENY")
    path.write_text("\n".join(lines) + "\n")
    result = audit.verify()
    assert result["ok"] is False


def test_s29_audit_gap_detected(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "fabric_audit_file", str(tmp_path / "a.jsonl"))
    from app.fabric import audit

    audit.record("shell", "run", "a", "AUTO", True)
    audit.record("shell", "run", "b", "AUTO", True)
    audit.record("shell", "run", "c", "AUTO", True)
    path = tmp_path / "a.jsonl"
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n")
    result = audit.verify()
    assert result["ok"] is False


async def test_s29_dispatch_appends_audit_without_raw_secrets(
    db_session: AsyncSession, fabric_on, tmp_path
) -> None:
    from app.fabric import audit

    await _dispatch(db_session, "shell_run", {"command": "echo hi"})
    await _dispatch(db_session, "shell_run", {"command": "sudo x"})
    result = audit.verify()
    assert result["ok"] is True
    assert result["checked"] == 2
    raw = (tmp_path / "audit.jsonl").read_text()
    assert "sudo x" not in raw, "audit must not log raw commands"
    assert "echo hi" not in raw
