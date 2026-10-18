"""S37: real Builder jobs — toy spec, fix loop, delegation, stale resume."""

from __future__ import annotations

import time
from datetime import UTC
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
    from app.fabric import panic

    panic.clear()
    return home


def _toy_project(root: Path) -> Path:
    proj = root / "toycalc"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (proj / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    return proj


async def test_s37_toy_job_end_to_end(db_session: AsyncSession, fabric_on) -> None:
    """Plan -> apply (real pytest) -> receipt with test summary."""
    import sys

    from app.fabric import builder as lane

    proj = _toy_project(fabric_on / "Desktop")
    planned = lane.build_plan("toy calc", root=proj, project_dir=".",
                              steps=[{"name": "test",
                                      "cmd": f"{sys.executable} -m pytest -q"}])
    assert planned["ok"], planned
    assert planned["plan"]["project_dir"] == "."
    applied = lane.apply_plan(planned, root=proj)
    assert applied["ok"], applied
    summary = applied["evidence"]["test_summary"]
    assert summary.get("parsed") is True, applied["evidence"]
    assert summary.get("passed") == 1 and summary.get("failed") == 0


async def test_s37_fix_loop_retries_distinctly(fabric_on) -> None:
    from app.fabric import builder as lane

    root = fabric_on / "Desktop"
    # Fails once (missing flag file), passes after the first attempt creates it.
    script = root / "flaky.sh"
    script.write_text("#!/bin/sh\nif [ -f flag ]; then exit 0; else touch flag; exit 1; fi\n")
    script.chmod(0o755)
    planned = lane.build_plan("flaky", root=root,
                              steps=[{"name": "flaky", "cmd": "./flaky.sh"}])
    applied = lane.apply_plan(planned, root=root)
    assert applied["ok"], applied
    evidence = applied["evidence"]
    assert evidence["state"] == "done"


async def test_s37_fix_loop_gives_up_after_budget(fabric_on) -> None:
    from app.fabric import builder as lane

    root = fabric_on / "Desktop"
    planned = lane.build_plan("doomed", root=root,
                              steps=[{"name": "nope", "cmd": "exit 3"}])
    applied = lane.apply_plan(planned, root=root, fix_rounds=3)
    assert applied["ok"] is False
    steps = applied["evidence"]["steps"]
    assert len(steps) == 1 and len(steps[0]["attempts"]) == 3
    assert [a["strategy"] for a in steps[0]["attempts"]] == ["retry", "verbose", "timeout_x3"]


async def test_s37_stale_running_repaired_and_resumable(fabric_on) -> None:
    import sqlite3 as _sqlite

    from app.fabric import builder as lane

    root = fabric_on / "Desktop"
    planned = lane.build_plan("stale", root=root, steps=[{"name": "x"}])
    jid = planned["job_id"]
    conn = _sqlite.connect(str(root / "builder_jobs.db"))
    conn.execute("UPDATE jobs SET state=?, updated=? WHERE id=?",
                 ("running", time.time() - 3600, jid))
    conn.commit()
    conn.close()
    resumed = lane.resume_job(jid, root=root)
    assert resumed["evidence"]["state"] == "interrupted", resumed
    # A fresh apply takes over the repaired job instead of refusing.
    applied = lane.apply_plan({"plan": {"steps": [{"name": "x"}]}, "job_id": jid},
                              root=root)
    assert applied["ok"], applied


async def test_s37_fresh_running_refuses_takeover(fabric_on) -> None:
    import sqlite3 as _sqlite

    from app.fabric import builder as lane

    root = fabric_on / "Desktop"
    planned = lane.build_plan("busy", root=root, steps=[{"name": "x"}])
    jid = planned["job_id"]
    conn = _sqlite.connect(str(root / "builder_jobs.db"))
    conn.execute("UPDATE jobs SET state=?, updated=? WHERE id=?",
                 ("running", time.time(), jid))
    conn.commit()
    conn.close()
    applied = lane.apply_plan({"plan": {"steps": [{"name": "x"}]}, "job_id": jid},
                              root=root)
    assert applied["ok"] is False
    assert "already running" in applied["error"]


async def test_s37_delegation_uses_fake_agent_cli(
    fabric_on, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.fabric import builder as lane

    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "opencode"
    fake.write_text('#!/bin/sh\necho "AGENT-DID-IT"\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    assert lane.detect_agent_cli()[0] == "opencode"
    out = lane._run_agent_step({"name": "code", "agent": "write hello",
                                "cwd": str(fabric_on / "Desktop")},
                               fabric_on / "Desktop", timeout=60.0)
    assert out["ok"], out
    assert "AGENT-DID-IT" in out["evidence"]["out"]
    assert out["evidence"]["agent"] == "opencode"


async def test_s37_no_cli_falls_back_honestly(
    fabric_on, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.fabric import builder as lane

    monkeypatch.setattr(lane.shutil, "which", lambda *_a, **_k: None)
    assert lane.detect_agent_cli() is None
    out = lane._run_agent_step({"name": "code", "agent": "write hello"},
                               fabric_on / "Desktop")
    assert out["ok"] is False
    assert out["evidence"].get("fallback") == "internal"


async def test_s37_kernel_reaches_builder_lanes(
    db_session: AsyncSession, fabric_on, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Voice path: scripted model calls builder.job plan/apply through the kernel."""
    from app.cognitive.executor import execute_semantic
    from app.cognitive.session_store import CognitiveSession, reset_for_tests

    seen: dict[str, Any] = {}

    async def fake_dispatch(session, name, arguments, **kwargs):
        seen["name"] = name
        seen["args"] = dict(arguments or {})
        return {"ok": True, "spoken": "done."}

    monkeypatch.setattr("app.ev.tools.dispatch", fake_dispatch)
    reset_for_tests()
    try:
        out = await execute_semantic(
            db_session, "builder.job", {"op": "plan", "goal": "toy"},
            cognition=CognitiveSession(session_id="s37"),
            actor="master", live_session_id=None, steering_seen=0)
    finally:
        reset_for_tests()
    assert seen["name"] == "builder_job"
    assert out["ok"] is True


async def test_s37_code_act_builder_bridge(
    db_session: AsyncSession, fabric_on, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.cognitive.executor import execute_semantic
    from app.cognitive.session_store import CognitiveSession, reset_for_tests

    seen: dict[str, Any] = {}

    async def fake_dispatch(session, name, arguments, **kwargs):
        seen["name"] = name
        seen["args"] = dict(arguments or {})
        return {"ok": True, "spoken": "done."}

    monkeypatch.setattr("app.ev.tools.dispatch", fake_dispatch)
    reset_for_tests()
    try:
        out = await execute_semantic(
            db_session, "code.act", {"engine": "builder", "op": "plan", "goal": "g"},
            cognition=CognitiveSession(session_id="s37b"),
            actor="master", live_session_id=None, steering_seen=0)
    finally:
        reset_for_tests()
    assert seen["name"] == "builder_job"
    assert seen["args"]["op"] == "plan"
    assert out["ok"] is True


async def test_s37_apply_notifies_completion(
    db_session: AsyncSession, fabric_on, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.ev.tools import dispatch

    calls: list[dict] = []

    async def fake_notify(session, **kwargs):
        calls.append(kwargs)
        from types import SimpleNamespace

        return SimpleNamespace(id="n1")

    monkeypatch.setattr("app.notify.service.dispatch_notification", fake_notify)
    planned = await dispatch(db_session, "builder_job",
                             {"op": "plan", "goal": "n",
                              "steps": [{"name": "x"}]},
                             actor="master", allow_sensitive=True)
    approved = await dispatch(
        db_session, "builder_job",
        {"op": "apply", "plan": planned.result["plan"],
         "job_id": planned.result["job_id"]},
        actor="master", allow_sensitive=True,
        confirmation=_approval())
    assert approved.result["ok"] is True
    assert calls and calls[0]["kind"] == "builder"


def _approval():
    from datetime import datetime, timedelta

    from app.ev.policy import Confirmation

    now = datetime.now(UTC)
    return Confirmation(factor="http_approve", confirmed=True,
                        target="builder_job", issued_at=now,
                        expires_at=now + timedelta(seconds=120))
