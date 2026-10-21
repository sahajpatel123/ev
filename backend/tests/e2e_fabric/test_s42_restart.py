"""S42: isolated restart — shell reattach, parked approvals, no phantom success.

Proved without touching the owner's launchd jobs or production database:
a new ShellSessions object is a new process against the same registry, and a
new SQLAlchemy session is a new connection against the same scratch database.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from uuid import UUID

import pytest
from httpx import AsyncClient

from app.config import settings

pytestmark_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux required")


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


@pytestmark_tmux
def test_s42_shell_reattach_reads_real_output(tmp_path: Path) -> None:
    from app.fabric.shell_sessions import ShellSessions

    started = ShellSessions(root=tmp_path).start(
        "echo S42-MARKER; sleep 30", confirm=True
    )
    assert started["ok"], started
    sid = started["session_id"]
    try:
        # Drop the first object. The registry file is the only memory.
        deadline = time.time() + 5
        output = ""
        alive = False
        while time.time() < deadline:
            fresh = ShellSessions(root=tmp_path)
            again = fresh.reattach(sid)
            output = str(again.get("output") or "")
            alive = bool(again.get("ok"))
            if alive and "S42-MARKER" in output:
                break
            time.sleep(0.2)
        assert alive, "session did not survive a new ShellSessions instance"
        assert "S42-MARKER" in output, output
    finally:
        ShellSessions(root=tmp_path).cancel(sid)


async def test_s42_parked_approval_survives_new_connection(
    client: AsyncClient, fabric_on
) -> None:
    """A CONFIRM effect stays pending across a new DB connection and does not run."""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import ApprovedAction

    resp = await client.post(
        "/v1/fabric/exec",
        json={"tool": "shell_run", "args": {"command": "touch s42-should-not-exist"}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False, body
    action_id = body["result"]["action_id"]
    assert not (fabric_on / "Desktop" / "s42-should-not-exist").exists()

    # The request session is a different connection from this one.
    async with SessionLocal() as restarted:
        row = await restarted.get(ApprovedAction, UUID(action_id))
        assert row is not None
        assert row.status == "pending"
        pending = (
            await restarted.execute(
                select(ApprovedAction).where(ApprovedAction.status == "pending")
            )
        ).scalars().all()
        assert any(str(item.id) == action_id for item in pending)

    listed = await client.get("/v1/fabric/approvals/pending")
    assert listed.status_code == 200, listed.text
    ids = [item["id"] for item in listed.json()["approvals"]]
    assert action_id in ids
    # Still not executed: a second read after the "restart" sees the same hold.
    async with SessionLocal() as restarted:
        row = await restarted.get(ApprovedAction, UUID(action_id))
        assert row is not None and row.status == "pending"


def test_s42_success_requires_readback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A write whose bytes do not match the world is not a verified success."""
    import app.fabric.files as files

    monkeypatch.setattr(files, "_guarded_read_bytes", lambda _path: b"different")
    receipt = files.create("note.txt", "hello", root=tmp_path)
    assert receipt["ok"] is False
    assert receipt["verified"] is False
    assert "mismatch" in str(receipt["error"])

    # The matching path verifies only after the file on disk equals the write.
    monkeypatch.undo()
    good = files.create("note.txt", "hello", root=tmp_path)
    assert good["ok"] is True and good["verified"] is True, good
    assert (tmp_path / "note.txt").read_text() == "hello"
    assert good["evidence"]["sha256"]
