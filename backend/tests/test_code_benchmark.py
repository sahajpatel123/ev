"""Unscripted coding benchmark: outcome-graded tasks through the real code lane.

Two layers:

* Offline tests (always run): heuristic honesty, jail refusals, seatbelt
  confinement. Strict assertions — these gate the suite.
* Live benchmark (needs the owner env, otherwise skipped): five
  plain-English tasks through ``run_code_job`` with the real MiMo brain,
  graded on OUTCOMES (file bytes, program stdout, exit codes) — never on
  tool traces. It measures and reports; it does not fail the suite when the
  model underperforms, because model quality is a measurement, not a gate.
  Expect several minutes and real OpenRouter spend. Run it as::

      set -a; source /Users/sahajpatel/Code/ev/.env
      source ~/.ev/secrets/production.env; set +a
      EV_TEST_USE_LIVE_CHAT=1 .venv/bin/python -m pytest \
          tests/test_code_benchmark.py::test_live_coding_benchmark -q -s

  (conftest blanks provider keys unless ``EV_TEST_USE_LIVE_CHAT=1``, and the
  brain path needs ``EV_CHAT_PROVIDER=mimo`` from the root .env.)

Nothing here authors tool calls or pre-seeds expected file bytes for the
task under test (planted bug/config fixtures are the task *input*).
"""

from __future__ import annotations

import json
import shutil
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
    work.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "code_workspace", str(work))
    return work


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
    assert result.get("error") == "mimo_unavailable"
    assert result.get("degraded") is True
    assert not result.get("files_changed")


async def test_key_with_offline_provider_refused_without_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Key + echo provider refuses as mimo_unavailable, never crashes.

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
    assert result.get("error") == "mimo_unavailable"
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


class _RecordingMimoProvider:
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
    provider = _RecordingMimoProvider(
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


LIVE_TASKS: list[tuple[str, str, TaskSetup | None, Callable[[Path, dict[str, Any]], tuple[bool, str]]]] = [
    ("write_add", "write a python script add.py with an add function and print add(2, 3)", None, _verify_add),
    ("fix_bug", "fix the bug in buggy.py so it prints 60", _plant_buggy, _verify_fix),
    ("edit_file", "patch greeting.txt: replace Mars with Venus", _plant_greeting, _verify_edit),
    ("explain", "explain what the calc.py python file does", _plant_calc, _verify_explain),
    ("run_tests", "run the tests", _plant_tests, _verify_tests),
]


@needs_key
async def test_live_coding_benchmark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Grade the live brain on outcomes. Measurement only — never gates green."""

    from app.ev import luna_code
    from app.ev.code_runtime import clear_sticky_project

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
