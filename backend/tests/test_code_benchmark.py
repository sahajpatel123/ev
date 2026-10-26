"""Unscripted coding benchmark: outcome-graded tasks through the real code lane.

Two layers:

* Offline tests (always run): heuristic honesty, jail refusals, seatbelt
  confinement. Strict assertions — these gate the suite.
* Live benchmark (needs the owner env, otherwise skipped): five
  plain-English tasks through ``run_code_job`` with the real brain brain,
  graded on OUTCOMES (file bytes, program stdout, exit codes) — never on
  tool traces. It measures and reports; it does not fail the suite when the
  model underperforms, because model quality is a measurement, not a gate.
  Expect several minutes and real OpenRouter spend. Run it as::

      set -a; source /Users/sahajpatel/Code/ev/.env
      source ~/.ev/secrets/production.env; set +a
      EV_TEST_USE_LIVE_CHAT=1 .venv/bin/python -m pytest \
          tests/test_code_benchmark.py::test_live_coding_benchmark -q -s

  (conftest blanks provider keys unless ``EV_TEST_USE_LIVE_CHAT=1``, and the
  brain path needs ``EV_CHAT_PROVIDER=brain`` from the root .env.)

Nothing here authors tool calls or pre-seeds expected file bytes for the
task under test (planted bug/config fixtures are the task *input*).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app.config import settings
from app.ev.code_runtime import CodeJailError, run_argv
from app.ev.luna_code import run_code_job
from app.gateway.roles import text_role_available

needs_key = pytest.mark.skipif(
    not text_role_available(),
    reason="live benchmark needs EV_OPENROUTER_API_KEY",
)


def _use_workspace(monkeypatch: pytest.MonkeyPatch, work: Path) -> Path:
    from app.ev.code_runtime import set_active_project

    work.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "code_workspace", str(work))
    # The jail prefers the active project root over the setting, and
    # use_project() binds it persistently by design — so other test files
    # can leak one in. Bind explicitly for hermeticity.
    set_active_project(work)
    return work


@pytest.fixture(autouse=True)
def _clear_active_project():
    from app.ev.code_runtime import set_active_project

    set_active_project(None)
    yield
    set_active_project(None)


async def test_offline_fib_is_verified_not_canned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Offline fib: real file + real exit 0 + real output, marked heuristic."""

    work = _use_workspace(monkeypatch, tmp_path / "ws")
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    result = await run_code_job(
        "create a fibonacci function in python", session_key="bench-offline-fib"
    )
    assert result.get("ok") is True
    assert result.get("brain") == "heuristic"
    assert (work / "fibonacci.py").is_file()
    runs = [run for run in (result.get("runs") or []) if run.get("exit_code") == 0]
    assert runs, result
    assert "21" in " ".join(str(run.get("stdout") or "") for run in runs)


async def test_offline_project_edit_refused_without_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a key, a real edit is refused honestly — never faked."""

    _use_workspace(monkeypatch, tmp_path / "ws")
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    result = await run_code_job(
        "refactor the authentication module to use short-lived tokens",
        session_key="bench-offline-refuse",
    )
    assert result.get("ok") is False
    assert result.get("error") == "brain_unavailable"
    assert result.get("degraded") is True
    assert not result.get("files_changed")


async def test_key_with_offline_provider_refused_without_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Key + echo provider refuses as brain_unavailable, never crashes.

    Regression: the brain loop assumed complete_raw exists; EchoProvider has
    none, so a key with an offline provider died with AttributeError and
    fell through to code_incomplete. Now the lane refuses cleanly up front.
    """

    work = _use_workspace(monkeypatch, tmp_path / "ws")
    monkeypatch.setattr(settings, "openrouter_api_key", "test-key-present")
    monkeypatch.setattr(settings, "chat_provider", "echo")
    result = await run_code_job(
        "refactor the authentication module to use short-lived tokens",
        session_key="bench-guard",
    )
    assert result.get("ok") is False
    assert result.get("error") == "brain_unavailable"
    assert not result.get("files_changed")
    assert list(work.iterdir()) == []


@pytest.mark.parametrize(
    "argv",
    [
        ["python3", "-c", "print(1)"],
        ["node", "-e", "console.log(1)"],
        ["rm", "-rf", "/"],
        ["bash", "-c", "id"],
        ["git", "push"],
        ["python3", "ok.py; rm -rf /"],
        ["python3", "ok.py | cat /etc/passwd"],
    ],
)
def test_jail_refuses_dangerous_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    from app.ev import code_runtime

    _use_workspace(monkeypatch, tmp_path / "ws")
    with pytest.raises(CodeJailError):
        code_runtime.run_argv(argv)


@pytest.mark.parametrize("rel", ["../escape.txt", "/etc/passwd", "~/.ssh/id_rsa"])
def test_jail_refuses_path_escapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rel: str
) -> None:
    from app.ev import code_runtime

    _use_workspace(monkeypatch, tmp_path / "ws")
    with pytest.raises(CodeJailError):
        code_runtime.write_file(rel, "x")


def test_jail_refuses_secret_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.ev import code_runtime

    work = _use_workspace(monkeypatch, tmp_path / "ws")
    (work / ".env").write_text("EV_MASTER_KEY=[REDACTED]", encoding="utf-8")
    with pytest.raises(CodeJailError):
        code_runtime.read_file(".env")


def test_transaction_applies_all_ops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.ev import code_runtime

    work = _use_workspace(monkeypatch, tmp_path / "ws")
    (work / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
    (work / "old.py").write_text("print('old')\n", encoding="utf-8")
    (work / "gone.py").write_text("print('gone')\n", encoding="utf-8")
    result = code_runtime.apply_transaction(
        [
            {"op": "write", "path": "b.py", "content": "VALUE = 2\n"},
            {"op": "replace", "path": "a.py", "old": "VALUE = 1", "new": "VALUE = 10"},
            {"op": "move", "src": "old.py", "dst": "new.py"},
            {"op": "delete", "path": "gone.py"},
        ]
    )
    assert result.get("ok") is True
    assert result.get("ops") == 4
    assert (work / "b.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert (work / "a.py").read_text(encoding="utf-8") == "VALUE = 10\n"
    assert (work / "new.py").read_text(encoding="utf-8") == "print('old')\n"
    assert not (work / "old.py").exists()
    assert not (work / "gone.py").exists()


def test_transaction_rolls_back_on_mid_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.ev import code_runtime

    work = _use_workspace(monkeypatch, tmp_path / "ws")
    (work / "keep.py").write_text("ORIGINAL = True\n", encoding="utf-8")
    (work / "stay.py").write_text("print('stay')\n", encoding="utf-8")
    # The replace validates (stay.py exists with that text) but fails after
    # the move renames it away — proving mid-apply rollback, not pre-check.
    with pytest.raises(CodeJailError, match="rolled back"):
        code_runtime.apply_transaction(
            [
                {"op": "write", "path": "fresh.py", "content": "x = 1\n"},
                {"op": "move", "src": "stay.py", "dst": "moved.py"},
                {"op": "replace", "path": "stay.py", "old": "print('stay')", "new": "x"},
            ]
        )
    assert (work / "keep.py").read_text(encoding="utf-8") == "ORIGINAL = True\n"
    assert (work / "stay.py").read_text(encoding="utf-8") == "print('stay')\n"
    assert not (work / "fresh.py").exists()
    assert not (work / "moved.py").exists()


def test_move_delete_refusals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.ev import code_runtime

    work = _use_workspace(monkeypatch, tmp_path / "ws")
    (work / "a.py").write_text("x\n", encoding="utf-8")
    (work / "b.py").write_text("y\n", encoding="utf-8")
    (work / "sub").mkdir()
    with pytest.raises(CodeJailError):
        code_runtime.move_path("../escape.py", "x.py")
    with pytest.raises(CodeJailError, match="already exists"):
        code_runtime.move_path("a.py", "b.py")
    with pytest.raises(CodeJailError):
        code_runtime.move_path("a.py", ".env")
    with pytest.raises(CodeJailError):
        code_runtime.delete_path("sub")
    with pytest.raises(CodeJailError):
        code_runtime.delete_path("missing.py")
    assert (work / "a.py").read_text(encoding="utf-8") == "x\n"


@pytest.mark.skipif(
    shutil.which("sandbox-exec") is None, reason="needs macOS sandbox-exec"
)
def test_seatbelt_confines_writes_to_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = _use_workspace(monkeypatch, tmp_path / "ws")
    outside = Path("/Users/Shared/ev-bench-outside.txt")
    if outside.exists():
        pytest.skip("escape target already exists; refusing to touch it")
    (work / "escape.py").write_text(
        f'open({str(outside)!r}, "w").write("escape-probe")\n',
        encoding="utf-8",
    )
    try:
        ran = run_argv(["python3", "escape.py"])
    finally:
        # If confinement ever fails, remove only our own probe bytes.
        if outside.is_file() and outside.read_text(encoding="utf-8", errors="replace") == "escape-probe":
            outside.unlink()
    assert ran.get("isolation") == "seatbelt"
    assert ran.get("ok") is False
    assert not outside.exists()


@pytest.mark.skipif(
    shutil.which("sandbox-exec") is None, reason="needs macOS sandbox-exec"
)
def test_seatbelt_blocks_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work = _use_workspace(monkeypatch, tmp_path / "ws")
    (work / "net.py").write_text(
        "import socket\n"
        "socket.create_connection((\"8.8.8.8\", 53), timeout=5)\n"
        'print("net-open")\n',
        encoding="utf-8",
    )
    ran = run_argv(["python3", "net.py"])
    assert ran.get("isolation") == "seatbelt"
    assert ran.get("network") == "blocked"
    assert ran.get("ok") is False
    assert "net-open" not in (ran.get("stdout") or "")


def test_process_mode_reports_honestly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = _use_workspace(monkeypatch, tmp_path / "ws")
    monkeypatch.setattr(settings, "code_os_sandbox", "process")
    (work / "hi.py").write_text('print("hi")\n', encoding="utf-8")
    ran = run_argv(["python3", "hi.py"])
    assert ran.get("ok") is True
    assert ran.get("isolation") == "process"
    assert ran.get("network") == "unrestricted"


def test_unknown_sandbox_mode_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_workspace(monkeypatch, tmp_path / "ws")
    monkeypatch.setattr(settings, "code_os_sandbox", "bogus")
    with pytest.raises(CodeJailError, match="EV_CODE_OS_SANDBOX"):
        run_argv(["python3", "--version"])


def test_memory_budget_kills_hog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = _use_workspace(monkeypatch, tmp_path / "ws")
    monkeypatch.setattr(settings, "code_max_rss_mb", 64)
    (work / "hog.py").write_text(
        "import time\nx = bytearray(256 * 1024 * 1024)\nx[::4096] = b'\\0' * 65536\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    with pytest.raises(CodeJailError, match="memory budget"):
        run_argv(["python3", "hog.py"])


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS seatbelt tightening")
def test_auto_mode_fails_closed_on_macos_without_seatbelt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.ev import code_runtime

    _use_workspace(monkeypatch, tmp_path / "ws")
    monkeypatch.setattr(settings, "code_os_sandbox", "auto")
    monkeypatch.setattr(code_runtime, "_seatbelt_available", lambda: False)
    with pytest.raises(CodeJailError, match="refusing to run unsandboxed"):
        run_argv(["python3", "--version"])


def test_run_timeout_message_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = _use_workspace(monkeypatch, tmp_path / "ws")
    (work / "slow.py").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    with pytest.raises(CodeJailError, match="timed out after 1s"):
        run_argv(["python3", "slow.py"], timeout_seconds=1)


def test_symbol_map_outline_and_find(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.ev.code_map import file_outline, find_symbol, invalidate_code_map

    work = _use_workspace(monkeypatch, tmp_path / "ws")
    invalidate_code_map(work.resolve())
    (work / "calc.py").write_text(
        "class Calc:\n    def tax(self, price):\n        return price\n\ndef helper():\n    return 1\n",
        encoding="utf-8",
    )
    (work / "app.js").write_text("function boot() {\n  return true;\n}\n", encoding="utf-8")
    outline = file_outline("calc.py")
    assert outline.get("ok") is True
    names = {(item.get("name"), item.get("kind")) for item in outline.get("symbols") or []}
    assert ("Calc", "class") in names
    assert ("tax", "function") in names
    assert ("helper", "function") in names
    found = find_symbol("boot")
    assert found.get("ok") is True
    assert {"path": "app.js", "kind": "def", "line": 1} in found.get("hits")
    assert find_symbol("nope_missing").get("hits") == []


def test_symbol_map_refreshes_on_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    from app.ev.code_map import find_symbol, invalidate_code_map

    work = _use_workspace(monkeypatch, tmp_path / "ws")
    invalidate_code_map(work.resolve())
    target = work / "mod.py"
    target.write_text("def alpha():\n    return 1\n", encoding="utf-8")
    assert len(find_symbol("alpha").get("hits")) == 1
    # Forced mtime change: the cache must pick up the edit, not go stale.
    time.sleep(0.02)
    target.write_text("def beta():\n    return 2\n", encoding="utf-8")
    os.utime(target, (time.time() + 5, time.time() + 5))
    assert find_symbol("alpha").get("hits") == []
    assert len(find_symbol("beta").get("hits")) == 1


def test_parse_diagnostics_reads_check_output() -> None:
    from app.ev.luna_code import parse_diagnostics

    ruff = "calc.py:3:9: F841 Local variable `x` is assigned but never used\n"
    hits = parse_diagnostics(ruff, "")
    assert hits and hits[0]["path"] == "calc.py" and hits[0]["line"] == 3
    assert "F841" in (hits[0].get("message") or "")

    mypy = "app/models.py:42: error: Incompatible return value  [return-value]\n"
    hits = parse_diagnostics("", mypy)
    assert hits and hits[0]["path"] == "app/models.py" and hits[0]["line"] == 42

    tb = (
        'Traceback (most recent call last):\n'
        '  File "/tmp/ws/test_calc.py", line 7, in test_tax\n'
        '  File "/tmp/ws/calc.py", line 2, in tax\n'
        '    return proce * rate\n'
        "NameError: name 'proce' is not defined\n"
    )
    hits = parse_diagnostics(tb, "")
    paths = {(hit["path"], hit["line"]) for hit in hits}
    assert ("/tmp/ws/test_calc.py", 7) in paths
    assert ("/tmp/ws/calc.py", 2) in paths

    stdlib = '  File "/usr/lib/python3.12/unittest.py", line 10, in run\n'
    assert parse_diagnostics(stdlib, "") == []
    assert parse_diagnostics("all good", "") == []


def test_fix_nudge_names_failing_locations() -> None:
    from app.ev.luna_code import _BRAIN_FIX_NUDGE, _fix_nudge_with_diagnostics

    runs = [
        {
            "argv": ["python3", "-m", "pytest", "-q"],
            "exit_code": 1,
            "ok": False,
            "stdout": "",
            "stderr": "",
            "stdout_tail": "test_calc.py:7: assert 4.0 == 5.0\n",
            "stderr_tail": "",
        }
    ]
    nudge = _fix_nudge_with_diagnostics(runs)
    assert nudge.startswith(_BRAIN_FIX_NUDGE)
    assert "test_calc.py:7" in nudge
    assert _fix_nudge_with_diagnostics([]) == _BRAIN_FIX_NUDGE


def test_remembered_job_carries_versioned_plan() -> None:
    from app.ev import luna_code

    luna_code.remember_code_job(
        {
            "workspace": "/tmp/ws",
            "project": "ws",
            "files_changed": ["a.py"],
            "spoken": "did a",
            "ok": True,
            "goal": "refactor auth",
            "runs": [],
            "plan": [
                {"step": "Map the repo", "status": "done"},
                {"step": "Implement", "status": "doing"},
                {"step": "Verify", "status": "pending"},
            ],
        },
        session_key="bench-plan",
    )
    stored = luna_code.last_code_job("bench-plan")
    assert stored is not None
    assert stored.get("schema_version") == 2
    assert [item["status"] for item in stored.get("plan")] == ["done", "doing", "pending"]
    plan = luna_code.load_code_plan("bench-plan")
    assert plan["done"] == ["Map the repo"]
    assert plan["next"] == "Implement"
    assert plan["remaining"] == 2
    assert plan["total"] == 3


def test_expand_code_goal_includes_plan_progress() -> None:
    from app.ev.luna_code import expand_code_goal

    prior = {
        "files": ["a.py"],
        "workspace": "/tmp/ws",
        "project": "ws",
        "goal": "refactor auth",
        "plan": [
            {"step": "Map the repo", "status": "done"},
            {"step": "Implement", "status": "doing"},
        ],
    }
    out = expand_code_goal("now run the tests", prior)
    assert "plan done: Map the repo" in out
    assert "plan next: Implement" in out


def test_stalled_job_recovers_then_parks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.ev import luna_code
    from app.memory.paths import atomic_write_json, read_json

    monkeypatch.setattr(settings, "memory_dir", str(tmp_path / "mem"))
    running = luna_code._running_code_path()
    pending = luna_code._pending_code_path()
    ready = luna_code._ready_code_path()
    assert luna_code.recover_stalled_code_job() == "none"

    atomic_write_json(
        running,
        {"kind": "intern", "goal": "do things", "session_key": "owner", "attempts": 1},
    )
    assert luna_code.recover_stalled_code_job() == "recovered"
    assert not running.exists()
    assert (read_json(pending) or {}).get("attempts") == 2
    status = luna_code.code_intern_status()
    assert status["pending"] is True and status["pending_attempts"] == 2

    atomic_write_json(
        running,
        {"kind": "intern", "goal": "do things", "session_key": "owner", "attempts": 3},
    )
    assert luna_code.recover_stalled_code_job() == "parked"
    assert not running.exists()
    assert not pending.exists()
    receipt = read_json(ready) or {}
    assert receipt.get("ok") is False
    assert "parked" in str(receipt.get("spoken") or "")


def test_plan_receipt_names_next_step() -> None:
    from app.ev.luna_code import shape_code_spoken

    spoken = shape_code_spoken(
        {
            "ok": True,
            "workspace": "/tmp/ws",
            "files_changed": ["a.py"],
            "runs": [{"ok": True, "exit_code": 0, "stdout": "ok"}],
            "goal": "refactor auth",
            "plan": [
                {"step": "Map the repo", "status": "done"},
                {"step": "Implement", "status": "doing"},
                {"step": "Verify", "status": "pending"},
            ],
        }
    )
    assert "Step 2 of 3 is next: Implement" in spoken


def test_file_explain_classifier() -> None:
    from app.ev.luna_code import looks_like_code_explain, looks_like_code_file_explain

    for goal in (
        "explain what the calc.py python file does",
        "describe what app.ts does",
        "summarize server.go for me",
        "what does tax.py do",
    ):
        assert looks_like_code_file_explain(goal), goal
        assert looks_like_code_explain(goal), goal
    for goal in (
        "explain gravity",
        "write a python script that prints hello",
        "patch greeting.txt: replace Mars with Venus",
        "tell me about my chats",
    ):
        assert not looks_like_code_file_explain(goal), goal


class _RecordingBrainProvider:
    """Scripted brain that records which tools it was offered."""

    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = list(replies)
        self.offered: list[str] = []

    async def complete_raw(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        response_format: dict[str, Any] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.offered = [
            str(item.get("function", {}).get("name") or "")
            for item in (tools or [])
        ]
        if self.replies:
            return self.replies.pop(0)
        return {"choices": [{"message": {"content": "done"}}]}


def _tool_reply(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)},
                        }
                    ],
                }
            }
        ]
    }


def _text_reply(text: str) -> dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


async def test_file_explain_gets_read_only_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file explain reads, speaks, and is never offered a write tool."""

    from app.ev.code_runtime import clear_sticky_project

    work = _use_workspace(monkeypatch, tmp_path / "ws")
    (work / "calc.py").write_text(
        "def tax(price, rate=0.2):\n    return round(price * rate, 2)\n",
        encoding="utf-8",
    )
    clear_sticky_project()
    monkeypatch.setattr("app.gateway.roles.text_role_available", lambda: True)
    provider = _RecordingBrainProvider(
        [
            _tool_reply("c1", "read_file", {"path": "calc.py"}),
            _text_reply("calc.py computes sales tax with a default 20 percent rate."),
        ]
    )
    monkeypatch.setattr("app.gateway.roles.require_code_provider", lambda: provider)
    result = await run_code_job(
        "explain what the calc.py python file does", session_key="bench-file-explain"
    )
    assert result.get("ok") is True, result
    assert result.get("files_changed") == []
    assert "tax" in str(result.get("spoken") or "").lower()
    assert "write_file" not in provider.offered
    assert "replace_in_file" not in provider.offered
    assert "run_command" not in provider.offered
    assert "read_file" in provider.offered


# --- Live unscripted benchmark (key-gated, measurement only) ---


def _verify_add(work: Path, result: dict[str, Any]) -> tuple[bool, str]:
    target = work / "add.py"
    if not target.is_file():
        return False, "add.py missing"
    ran = run_argv(["python3", "add.py"])
    ok = ran.get("exit_code") == 0 and "5" in (ran.get("stdout") or "")
    return ok, f"exit={ran.get('exit_code')} stdout={(ran.get('stdout') or '').strip()[:60]!r}"


def _verify_fix(work: Path, result: dict[str, Any]) -> tuple[bool, str]:
    ran = run_argv(["python3", "buggy.py"])
    ok = ran.get("exit_code") == 0 and "60" in (ran.get("stdout") or "")
    return ok, f"exit={ran.get('exit_code')} stdout={(ran.get('stdout') or '').strip()[:60]!r}"


def _verify_edit(work: Path, result: dict[str, Any]) -> tuple[bool, str]:
    text = (work / "greeting.txt").read_text(encoding="utf-8") if (work / "greeting.txt").is_file() else ""
    ok = "Venus" in text and "Mars" not in text
    return ok, f"greeting={text.strip()[:60]!r}"


def _verify_explain(work: Path, result: dict[str, Any]) -> tuple[bool, str]:
    ok = not result.get("files_changed") and bool(str(result.get("spoken") or "").strip())
    return ok, f"files_changed={result.get('files_changed')} spoken_len={len(str(result.get('spoken') or ''))}"


def _verify_tests(work: Path, result: dict[str, Any]) -> tuple[bool, str]:
    runs = list(result.get("runs") or [])
    ok = any(run.get("exit_code") == 0 for run in runs)
    return ok, f"runs={len(runs)} clean={[r.get('exit_code') for r in runs][:4]}"


def _verify_rename(work: Path, result: dict[str, Any]) -> tuple[bool, str]:
    helper = (work / "helpers.py").is_file()
    gone = not (work / "util.py").exists()
    main = (work / "main.py").read_text(encoding="utf-8") if (work / "main.py").is_file() else ""
    ran = run_argv(["python3", "main.py"])
    ok = (
        helper
        and gone
        and "helpers" in main
        and ran.get("exit_code") == 0
        and "42" in (ran.get("stdout") or "")
    )
    return ok, f"helpers={helper} old_gone={gone} run_exit={ran.get('exit_code')}"


def _verify_suite(work: Path, result: dict[str, Any]) -> tuple[bool, str]:
    ran = run_argv(["python3", "-m", "pytest", "-q"])
    ok = ran.get("exit_code") == 0
    return ok, f"pytest_exit={ran.get('exit_code')}"


TaskSetup = Callable[[Path], None]


def _plant_buggy(work: Path) -> None:
    (work / "buggy.py").write_text(
        "def total(xs):\n    return suum(xs)\n\nprint(total([10, 20, 30]))\n",
        encoding="utf-8",
    )


def _plant_greeting(work: Path) -> None:
    (work / "greeting.txt").write_text("Hello, Mars.\n", encoding="utf-8")


def _plant_calc(work: Path) -> None:
    (work / "calc.py").write_text(
        "def tax(price, rate=0.2):\n    return round(price * rate, 2)\n",
        encoding="utf-8",
    )


def _plant_tests(work: Path) -> None:
    (work / "test_sample.py").write_text(
        "def test_one():\n    assert 1 + 1 == 2\n\ndef test_two():\n    assert 'ab'.upper() == 'AB'\n",
        encoding="utf-8",
    )


def _plant_rename(work: Path) -> None:
    (work / "util.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    (work / "main.py").write_text(
        "from util import answer\n\nprint(answer())\n", encoding="utf-8"
    )


def _plant_suite(work: Path) -> None:
    (work / "shapes.py").write_text(
        "def area(w, h):\n    return w + h\n", encoding="utf-8"
    )
    (work / "test_shapes.py").write_text(
        "from shapes import area\n\ndef test_area():\n    assert area(3, 4) == 12\n",
        encoding="utf-8",
    )


LIVE_TASKS: list[tuple[str, str, TaskSetup | None, Callable[[Path, dict[str, Any]], tuple[bool, str]]]] = [
    ("write_add", "write a python script add.py with an add function and print add(2, 3)", None, _verify_add),
    ("fix_bug", "fix the bug in buggy.py so it prints 60", _plant_buggy, _verify_fix),
    ("edit_file", "patch greeting.txt: replace Mars with Venus", _plant_greeting, _verify_edit),
    ("explain", "explain what the calc.py python file does", _plant_calc, _verify_explain),
    ("run_tests", "run the tests", _plant_tests, _verify_tests),
    ("rename_refactor", "refactor the python code: rename util.py to helpers.py and fix the import in main.py", _plant_rename, _verify_rename),
    ("fix_suite", "fix shapes.py so the failing test suite passes", _plant_suite, _verify_suite),
]


@needs_key
async def test_live_coding_benchmark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Grade the live brain on outcomes. Measurement only — never gates green."""

    from app.ev import luna_code
    from app.ev.code_runtime import clear_sticky_project, set_active_project

    # Tasks are independent: never inherit the owner's real last job (disk)
    # or a sibling task's (memory). Patched, not deleted: owner state on
    # disk is left untouched.
    monkeypatch.setattr(luna_code, "_load_last_code_job", lambda: None)
    report: list[dict[str, Any]] = []
    for name, goal, setup, verify in LIVE_TASKS:
        # Drop the sticky pin and prior-job memory so routing cannot inherit
        # another task's workspace ("run the tests" continues priors).
        clear_sticky_project()
        luna_code._LAST_CODE_JOBS.clear()
        work = tmp_path / name
        work.mkdir(parents=True, exist_ok=True)
        set_active_project(work)
        if setup is not None:
            setup(work)
        monkeypatch.setattr(settings, "code_workspace", str(work))
        started = time.monotonic()
        try:
            result = await run_code_job(goal, session_key=f"bench-live-{name}")
            passed, detail = verify(work, result)
            error = "" if passed else str(result.get("error") or "verify-failed")
        except Exception as exc:  # noqa: BLE001 - benchmark records, never raises
            passed, detail, error = False, "", f"{type(exc).__name__}: {exc}"
        elapsed = round(time.monotonic() - started, 1)
        report.append(
            {"task": name, "passed": passed, "seconds": elapsed, "detail": detail, "error": error}
        )
        print(f"[code-bench] {name}: {'PASS' if passed else 'FAIL'} {elapsed}s {detail} {error}")
    path = tmp_path / "code_benchmark_report.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[code-bench] report: {path}")
    with capsys.disabled():
        print(f"\n[code-bench] {sum(1 for r in report if r['passed'])}/{len(report)} passed")
    assert len(report) == len(LIVE_TASKS)
    assert path.is_file()
