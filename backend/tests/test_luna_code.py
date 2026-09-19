"""Evie coding broker: Luna brain, bounded workspace, live/chat dispatch."""

from __future__ import annotations

import asyncio
from datetime import UTC
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.code_runtime import CodeJailError, run_argv, workspace_root, write_file
from app.ev.luna_code import (
    execute_code_tool,
    looks_like_code_continue,
    looks_like_code_followup,
    looks_like_code_request,
    remember_code_job,
    run_code_job,
    spoken_code_followup,
)
from app.ev.policy import evaluate_policy
from app.ev.tool_select import F4_TARGET_SURFACE, LIVE_VOICE_TOOLS, resolve_live_action, select_tool
from app.ev.tools import dispatch, get_spec, list_tools
from app.voice.live.grok_voice import grok_voice_tools


async def _finish_live_code(live) -> None:
    task = getattr(live, "_owner_text_task", None)
    if task is not None:
        await task
    drain = getattr(live, "drain_code_job", None)
    if drain is not None:
        await drain()


async def _await_s2s(live, event):
    """Wait for transcript routing. emit() only schedules it for grok/realtime."""

    routed = await live.emit(event)
    if routed is not None:
        await routed
    return routed


def test_code_is_live_broker_not_a_shell() -> None:
    spec = get_spec("code")
    assert spec is not None
    assert spec["permission"] == "software:code"
    assert spec["risk_class"] == "R1"
    assert "code" in LIVE_VOICE_TOOLS
    assert "code" in F4_TARGET_SURFACE
    assert "execute_command" not in LIVE_VOICE_TOOLS
    assert "execute_command" not in {item["name"] for item in list_tools()}
    decision = evaluate_policy(
        "code",
        actor="master",
        channel="voice",
        training_wheels_complete=True,
        provider_connected=True,
        arguments={"goal": "write a python script that prints hi"},
    )
    assert decision.allowed is True
    assert decision.confirmation_required is False
    voice = evaluate_policy(
        "code",
        actor="voice",
        channel="voice",
        training_wheels_complete=True,
        provider_connected=True,
        arguments={"goal": "write a python script that prints hi"},
    )
    assert voice.allowed is True


def test_coding_intent_routing() -> None:
    assert looks_like_code_request("write a python script that prints hello world")
    assert looks_like_code_request("Evie, create a fibonacci function")
    assert looks_like_code_request("run hello.py")
    assert looks_like_code_request("write a javascript script that prints hello world")
    assert looks_like_code_request("write a ruby file that prints hello")
    assert looks_like_code_request("refactor the auth module in the ev repo")
    assert looks_like_code_request("work in the ev repo and add a retry")
    assert looks_like_code_request("open the ev repo")
    assert looks_like_code_request("fix the failing test")
    assert looks_like_code_request("run the tests in the demo project")
    assert looks_like_code_request("add a test for the code broker")
    assert looks_like_code_request("can you make me a python helper that grades scores")
    assert looks_like_code_request("I need a small python function that returns pass or fail")
    assert looks_like_code_request("make me a grader")
    assert looks_like_code_request("write hello.py")
    assert looks_like_code_request("write src/hello.py")
    assert looks_like_code_continue("run it")
    assert looks_like_code_continue("add a test")
    assert looks_like_code_continue("change 50 to 60")
    assert looks_like_code_continue("now run the tests")
    assert not looks_like_code_continue("run the tests in the demo project")
    assert not looks_like_code_followup("run it")
    assert not looks_like_code_request("can you make me a sandwich")
    assert not looks_like_code_request("write mom that I'm late")
    assert not looks_like_code_request("open Cursor")
    assert not looks_like_code_request("Open TextEdit and type hello world")
    assert not looks_like_code_request("run rm -rf /")
    assert select_tool("write a python script that prints hello world").selected == "code"
    assert resolve_live_action("write a python script that prints hello world") == (
        "code",
        {"goal": "write a python script that prints hello world"},
    )
    assert resolve_live_action("run hello.py") == ("code", {"goal": "run hello.py"})
    assert resolve_live_action("fix the bug in the ev repo") == (
        "code",
        {"goal": "fix the bug in the ev repo"},
    )
    assert resolve_live_action("run rm -rf /") is None
    assert resolve_live_action("how are you") is None
    wish = "tell me about the code that i have written in the wish workspace"
    assert looks_like_code_request(wish)
    assert resolve_live_action(wish) == ("code", {"goal": wish})
    assert select_tool(wish).selected == "code"
    info = "give me info about the wish workspace"
    assert looks_like_code_request(info)
    assert resolve_live_action(info) == ("code", {"goal": info})
    assert looks_like_code_request("what projects do I have")
    assert not looks_like_code_request("tell me about my conversations")
    assert not looks_like_code_request("tell me about my chats")
    assert not looks_like_code_request("I wish you would tell me about the weather")
    assert not looks_like_code_request("tell me about John")


def test_expand_code_goal_keeps_last_files() -> None:
    from app.ev.luna_code import expand_code_goal

    prior = {
        "files": ["score.py", "test_score.py"],
        "workspace": "/tmp/code-workspace",
        "project": "code-workspace",
        "goal": "make a grader",
    }
    out = expand_code_goal("add a test", prior)
    assert out.startswith("add a test")
    assert "score.py" in out
    assert "test_score.py" in out
    assert "Continue that work" in out


def test_workspace_jail_rejects_escape_and_forbidden_binaries(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "code_workspace", str(tmp_path))
    with pytest.raises(CodeJailError, match="escapes|relative"):
        write_file("../outside.py", "print(1)\n")
    with pytest.raises(CodeJailError, match="allowlisted"):
        run_argv(["rm", "-rf", "/"])
    with pytest.raises(CodeJailError, match="secret"):
        write_file(".env", "SECRET=1\n")
    denied = execute_code_tool("run_command", {"argv": ["bash", "-c", "echo pwned"]})
    assert denied["ok"] is False
    assert denied["error"] == "code_jail"
    inline = execute_code_tool("run_command", {"argv": ["python3", "-c", "print(1)"]})
    assert inline["ok"] is False
    node_eval = execute_code_tool("run_command", {"argv": ["node", "-e", "console.log(1)"]})
    assert node_eval["ok"] is False
    ruby_eval = execute_code_tool("run_command", {"argv": ["ruby", "-e", "puts 1"]})
    assert ruby_eval["ok"] is False
    php_eval = execute_code_tool("run_command", {"argv": ["php", "-r", "echo 1;"]})
    assert php_eval["ok"] is False
    npm = execute_code_tool("run_command", {"argv": ["npm", "install"]})
    assert npm["ok"] is False
    go_get = execute_code_tool("run_command", {"argv": ["go", "get", "golang.org/x/sys"]})
    assert go_get["ok"] is False
    dart_pub = execute_code_tool("run_command", {"argv": ["dart", "pub", "get"]})
    assert dart_pub["ok"] is False
    push = execute_code_tool("run_command", {"argv": ["git", "push"]})
    assert push["ok"] is False


@pytest.mark.asyncio
async def test_heuristic_writes_and_runs_hello(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "code_workspace", str(tmp_path))
    monkeypatch.setattr(settings, "openai_api_key", "")
    result = await run_code_job("write a python script that prints hello world")
    assert result["ok"] is True
    assert (tmp_path / "hello.py").read_text(encoding="utf-8") == "print('hello world')\n"
    assert any(item.get("exit_code") == 0 for item in result["runs"])
    spoken = (result["spoken"] or "").lower()
    assert "hello.py" in spoken
    assert "hello world" in spoken
    assert "ran" in spoken or "printed" in spoken
    assert result.get("workspace")


@pytest.mark.asyncio
async def test_dispatch_code_tool(db_session: AsyncSession, tmp_path: Path, monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "code_workspace", str(tmp_path))
    monkeypatch.setattr(settings, "openai_api_key", "")
    response = await dispatch(
        db_session,
        "code",
        {"goal": "write a python script that prints hi"},
        actor="master",
        allow_sensitive=True,
        channel="action",
    )
    assert response.ok is True
    body = response.result if isinstance(response.result, dict) else {}
    assert body.get("ok") is True
    assert (tmp_path / "hello.py").is_file()
    assert body.get("spoken")
    assert body.get("files_changed")
    assert body.get("evidence", {}).get("source") == "luna_code"


@pytest.mark.asyncio
async def test_chat_path_executes_code_before_speech(
    db_session: AsyncSession, tmp_path: Path, monkeypatch
) -> None:
    from app.config import settings
    from app.ev.turn import execute_requested_actions

    monkeypatch.setattr(settings, "code_workspace", str(tmp_path))
    monkeypatch.setattr(settings, "openai_api_key", "")
    receipts = await execute_requested_actions(
        db_session,
        "write a python script that prints hello world",
        actor="master",
        allow_sensitive=True,
    )
    assert receipts
    assert receipts[0].name == "code"
    assert receipts[0].ok is True
    assert (tmp_path / "hello.py").is_file()


@pytest.mark.asyncio
async def test_offline_unknown_job_is_honest(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "code_workspace", str(tmp_path))
    monkeypatch.setattr(settings, "openai_api_key", "")
    result = await run_code_job("refactor the auth module into a state machine")
    assert result["ok"] is False
    assert result.get("degraded") is True
    spoken = (result.get("spoken") or "").lower()
    assert "couldn't" in spoken or "unavailable" in spoken or "key" in spoken
    assert "wrote" not in spoken


@pytest.mark.asyncio
async def test_luna_loop_writes_and_runs_via_tools(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "code_workspace", str(tmp_path))
    monkeypatch.setattr(settings, "openai_api_key", "sk-test-luna")
    monkeypatch.setattr(settings, "code_model", "gpt-5.6-luna")

    class _Resp:
        def __init__(self, payload: dict, status: int = 200) -> None:
            self.status_code = status
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    class _FakeClient:
        posts = 0

        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def post(self, url, headers=None, json=None):
            _FakeClient.posts += 1
            if _FakeClient.posts == 1:
                return _Resp(
                    {
                        "output": [
                            {
                                "type": "function_call",
                                "call_id": "call_write",
                                "name": "write_file",
                                "arguments": '{"path":"hello.py","content":"print(\'hello world\')\\n"}',
                            }
                        ]
                    }
                )
            if _FakeClient.posts == 2:
                return _Resp(
                    {
                        "output": [
                            {
                                "type": "function_call",
                                "call_id": "call_run",
                                "name": "run_command",
                                "arguments": '{"argv":["python3","hello.py"]}',
                            }
                        ]
                    }
                )
            return _Resp(
                {
                    "output_text": "Wrote hello.py and ran it. Output: hello world",
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "Wrote hello.py and ran it. Output: hello world",
                                }
                            ],
                        }
                    ],
                }
            )

    monkeypatch.setattr("httpx.AsyncClient", _FakeClient)
    result = await run_code_job("write a python script that prints hello world")
    assert result["ok"] is True
    assert result["brain"] == "gpt-5.6-luna"
    assert (tmp_path / "hello.py").read_text(encoding="utf-8") == "print('hello world')\n"
    assert any(item.get("exit_code") == 0 for item in result["runs"])
    assert "hello world" in (result["spoken"] or "").lower()


def _seed_project(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir()
    (root / "mathy.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / "test_mathy.py").write_text(
        "from mathy import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    return root


def test_named_project_search_patch_and_sibling_jail(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings
    from app.ev.code_runtime import (
        replace_in_file,
        reset_active_project,
        search_text,
        select_project,
        set_active_project,
    )

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    code_home = tmp_path / "Code"
    ev_root = _seed_project(code_home / "ev")
    other = _seed_project(code_home / "other")
    monkeypatch.setattr(settings, "code_workspace", str(sandbox))
    monkeypatch.setattr(settings, "code_projects_root", str(code_home))
    from app.ev.code_runtime import clear_sticky_project

    clear_sticky_project()
    assert select_project("fix the add function in the ev repo") == ev_root.resolve()
    token = set_active_project(ev_root)
    try:
        hits = search_text(r"def add", ".", glob="*.py")
        assert hits["ok"] is True
        assert any(item["path"] == "mathy.py" for item in hits["hits"])
        patched = replace_in_file(
            "mathy.py",
            "    return a + b\n",
            "    return a + b + 0\n",
        )
        assert patched["ok"] is True
        with pytest.raises(CodeJailError, match="escapes|relative"):
            replace_in_file("../other/mathy.py", "return a + b", "return 0")
        assert "return a + b + 0" in (ev_root / "mathy.py").read_text(encoding="utf-8")
        assert "return a + b + 0" not in (other / "mathy.py").read_text(encoding="utf-8")
    finally:
        reset_active_project(token)


@pytest.mark.asyncio
async def test_offline_runs_tests_in_named_project(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    demo = _seed_project(tmp_path / "Code" / "demo")
    monkeypatch.setattr(settings, "code_workspace", str(sandbox))
    monkeypatch.setattr(settings, "code_projects_root", str(tmp_path / "Code"))
    monkeypatch.setattr(settings, "openai_api_key", "")
    result = await run_code_job("run the tests in the demo project")
    assert result["ok"] is True
    assert result.get("project") == "demo"
    assert (demo / "mathy.py").is_file()
    assert any(item.get("exit_code") == 0 for item in result["runs"])


@pytest.mark.asyncio
async def test_luna_loop_patches_existing_file(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "code_workspace", str(tmp_path))
    monkeypatch.setattr(settings, "code_projects_root", "")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test-luna")
    monkeypatch.setattr(settings, "code_model", "gpt-5.6-luna")
    (tmp_path / "util.py").write_text("def answer():\n    return 1\n", encoding="utf-8")

    class _Resp:
        def __init__(self, payload: dict, status: int = 200) -> None:
            self.status_code = status
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    class _FakeClient:
        posts = 0

        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def post(self, url, headers=None, json=None):
            _FakeClient.posts += 1
            if _FakeClient.posts == 1:
                return _Resp(
                    {
                        "output": [
                            {
                                "type": "function_call",
                                "call_id": "call_search",
                                "name": "search",
                                "arguments": '{"pattern":"return 1","glob":"*.py"}',
                            }
                        ]
                    }
                )
            if _FakeClient.posts == 2:
                return _Resp(
                    {
                        "output": [
                            {
                                "type": "function_call",
                                "call_id": "call_patch",
                                "name": "replace_in_file",
                                "arguments": (
                                    '{"path":"util.py","old":"    return 1\\n",'
                                    '"new":"    return 2\\n"}'
                                ),
                            }
                        ]
                    }
                )
            if _FakeClient.posts == 3:
                return _Resp(
                    {
                        "output": [
                            {
                                "type": "function_call",
                                "call_id": "call_run",
                                "name": "run_command",
                                "arguments": '{"argv":["python3","util.py"]}',
                            }
                        ]
                    }
                )
            return _Resp(
                {
                    "output_text": "Updated util.py so answer() returns 2.",
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "Updated util.py so answer() returns 2.",
                                }
                            ],
                        }
                    ],
                }
            )

    monkeypatch.setattr("httpx.AsyncClient", _FakeClient)
    result = await run_code_job("in this repo, change answer() to return 2")
    # python -c is jailed; the patch itself is the verified change.
    assert (tmp_path / "util.py").read_text(encoding="utf-8") == "def answer():\n    return 2\n"
    assert result["ok"] is True
    assert "util.py" in result["files_changed"]


def test_realtime_projection_can_advertise_code_without_shell() -> None:
    spec = get_spec("code")
    payload = grok_voice_tools([spec])
    names = {item["name"] for item in payload}
    assert "code" in names
    assert "execute_command" not in names
    assert workspace_root().exists()


@pytest.mark.asyncio
async def test_live_s2s_runs_code_from_owner_transcript(
    tmp_path: Path, monkeypatch
) -> None:
    """Realtime Mini often will not call `code`. The transcript must."""

    import json

    from app.config import settings
    from app.voice.live.events import FinalTranscriptEvent
    from app.voice.live.session import LiveSession

    monkeypatch.setattr(settings, "code_workspace", str(tmp_path))
    monkeypatch.setattr(settings, "code_projects_root", "")
    monkeypatch.setattr(settings, "openai_api_key", "")

    seen: list[tuple[str, dict, str]] = []
    spoken: list[str] = []
    cancelled = {"n": 0}

    async def runner(name: str, args: dict, call_id: str) -> str:
        seen.append((name, dict(args), call_id))
        result = await run_code_job(str(args.get("goal") or ""))
        return json.dumps(
            {
                "ok": result.get("ok"),
                "result": result,
                "spoken": result.get("spoken"),
            }
        )

    class _OpenAI:
        _provider = "openai"
        supports_function_calls = True
        _open_turn_id = "turn-code-1"
        _shadow_response_for_turn = None

        async def cancel(self) -> None:
            cancelled["n"] += 1

        async def speak_ack(self, text: str) -> bool:
            spoken.append(text)
            return True

        async def send_text(self, text: str) -> None:
            raise AssertionError(f"Mini must not receive the coding command: {text}")

    grok = _OpenAI()
    live = LiveSession(session_id="owner-code-talk", backchannel_enabled=False)
    live.run_live_tool = runner
    live.grok_voice = grok
    goal = "write a python script that prints hello world"
    try:
        await _await_s2s(
            live,
            FinalTranscriptEvent(at_ms=1, text=goal, provider="openai-realtime"),
        )
        await _finish_live_code(live)
        assert cancelled["n"] == 1
        assert grok._shadow_response_for_turn == "turn-code-1"
        assert seen == [("code", {"goal": goal}, "owner-code-exec")]
        assert (tmp_path / "hello.py").read_text(encoding="utf-8") == "print('hello world')\n"
        assert spoken
        assert "hello" in spoken[0].lower()
    finally:
        live.close()


def test_stale_intern_pending_does_not_mark_code_jail_busy(
    tmp_path: Path, monkeypatch
) -> None:
    from app.config import settings
    from app.ev.code_studio import spoken_studio_busy
    from app.ev.luna_code import (
        code_jail_busy,
        enqueue_code_intern,
        intern_in_flight,
        intern_worker_active,
    )

    monkeypatch.setattr(settings, "memory_dir", str(tmp_path / "mem"))
    enqueue_code_intern("leftover overnight job")
    assert intern_in_flight() is True
    assert intern_worker_active() is False
    assert code_jail_busy() is False
    busy = spoken_studio_busy()
    assert "running" not in busy.lower()
    assert "queued" in busy.lower() or "not running" in busy.lower()


def test_stale_running_studio_does_not_block_new_code(
    tmp_path: Path, monkeypatch
) -> None:
    from datetime import datetime, timedelta

    from app.config import settings
    from app.ev.code_studio import load_studio, maybe_handle_code_ops, save_studio
    from app.ev.luna_code import code_jail_busy

    monkeypatch.setattr(settings, "memory_dir", str(tmp_path / "mem"))
    ack = maybe_handle_code_ops("make a clothing site UI from scratch")
    assert ack
    studio = load_studio()
    assert studio is not None
    studio["status"] = "running"
    studio["updated_at"] = (datetime.now(UTC) - timedelta(days=5)).isoformat()
    save_studio(studio, claim=True)
    assert code_jail_busy() is False
    assert load_studio() is None
    assert maybe_handle_code_ops("write a python script that prints hello world") is None
    calc = maybe_handle_code_ops("create a calculator app UI")
    assert calc
    assert "queued" not in calc.lower()
    live = load_studio()
    assert live is not None
    assert live.get("kind") == "calculator"


@pytest.mark.asyncio
async def test_stale_intern_pending_does_not_block_live_hello(
    tmp_path: Path, monkeypatch
) -> None:
    import json

    from app.config import settings
    from app.ev.luna_code import enqueue_code_intern
    from app.voice.live.events import FinalTranscriptEvent
    from app.voice.live.session import LiveSession

    monkeypatch.setattr(settings, "code_workspace", str(tmp_path))
    monkeypatch.setattr(settings, "code_projects_root", "")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "memory_dir", str(tmp_path / "mem"))
    enqueue_code_intern("leftover overnight job")

    seen: list[tuple[str, dict, str]] = []
    spoken: list[str] = []

    async def runner(name: str, args: dict, call_id: str) -> str:
        seen.append((name, dict(args), call_id))
        result = await run_code_job(str(args.get("goal") or ""))
        return json.dumps(
            {
                "ok": result.get("ok"),
                "result": result,
                "spoken": result.get("spoken"),
            }
        )

    class _OpenAI:
        _provider = "openai"
        supports_function_calls = True
        _open_turn_id = "turn-code-stale"
        _shadow_response_for_turn = None

        async def cancel(self) -> None:
            return None

        async def speak_ack(self, text: str) -> bool:
            spoken.append(text)
            return True

        async def send_text(self, text: str) -> None:
            raise AssertionError(f"Mini must not receive the coding command: {text}")

    grok = _OpenAI()
    live = LiveSession(session_id="owner-code-stale-intern", backchannel_enabled=False)
    live.run_live_tool = runner
    live.grok_voice = grok
    goal = "write a python script that prints hello world"
    try:
        await _await_s2s(
            live,
            FinalTranscriptEvent(at_ms=1, text=goal, provider="openai-realtime"),
        )
        await _finish_live_code(live)
        assert seen == [("code", {"goal": goal}, "owner-code-exec")]
        assert (tmp_path / "hello.py").read_text(encoding="utf-8") == "print('hello world')\n"
        assert spoken
        assert "hello" in spoken[-1].lower()
        assert all("i'm running" not in item.lower() for item in spoken)
    finally:
        live.close()


@pytest.mark.asyncio
async def test_partial_code_transcript_cancels_mini_before_she_claims_a_write() -> None:
    from app.voice.live.events import PartialTranscriptEvent
    from app.voice.live.session import LiveSession

    cancelled = {"n": 0}

    class _OpenAI:
        _provider = "openai"
        supports_function_calls = True
        _open_turn_id = "turn-code-partial"
        _shadow_response_for_turn = None
        _response_active = True
        _assistant_open = True

        async def cancel(self) -> None:
            cancelled["n"] += 1

    live = LiveSession(session_id="owner-code-partial", backchannel_enabled=False)
    live.run_live_tool = lambda *_a, **_k: None
    live.grok_voice = _OpenAI()
    try:
        await live.emit(
            PartialTranscriptEvent(
                at_ms=1,
                text="write a python script that prints hello world",
                sequence=1,
            )
        )
        assert cancelled["n"] == 1
        assert live.grok_voice._shadow_response_for_turn == "turn-code-partial"
    finally:
        live.close()


@pytest.mark.asyncio
async def test_background_code_job_stashes_a_receipt(
    tmp_path: Path, monkeypatch
) -> None:
    from app.config import settings
    from app.ev.luna_code import peek_code_intern_receipt, run_code_job_and_notify

    monkeypatch.setattr(settings, "code_workspace", str(tmp_path))
    monkeypatch.setattr(settings, "code_projects_root", "")
    monkeypatch.setattr(settings, "memory_dir", str(tmp_path / "mem"))
    monkeypatch.setattr(settings, "openai_api_key", "")
    notes: list[str] = []
    monkeypatch.setattr(
        "app.ev.luna_code.schedule_background_code_notify",
        lambda spoken: notes.append(spoken),
    )
    await run_code_job_and_notify(
        "write a python script that prints hello world", session_key="owner"
    )
    assert (tmp_path / "hello.py").is_file()
    receipt = peek_code_intern_receipt()
    assert receipt
    assert "hello" in receipt.lower()
    assert notes
    assert "hello" in notes[0].lower()


@pytest.mark.asyncio
async def test_live_code_still_runs_when_muse_kernel_is_on(
    tmp_path: Path, monkeypatch
) -> None:
    """Muse kernel must not skip the coding jail and invent a spoken success."""

    import json

    from app.config import settings
    from app.voice.live.events import FinalTranscriptEvent
    from app.voice.live.session import LiveSession

    monkeypatch.setattr(settings, "code_workspace", str(tmp_path))
    monkeypatch.setattr(settings, "code_projects_root", "")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "cognitive_mode", "muse_kernel")
    monkeypatch.setattr("app.gateway.muse.muse_spark_key_loaded", lambda: False)
    kernel_calls = {"n": 0}

    async def boom_kernel(**_kwargs):
        kernel_calls["n"] += 1
        raise AssertionError("coding must not wait on the cognitive kernel")

    monkeypatch.setattr("app.cognitive.kernel.handle_turn_maybe_remote", boom_kernel)

    seen: list[tuple[str, dict, str]] = []
    spoken: list[str] = []

    async def runner(name: str, args: dict, call_id: str) -> str:
        seen.append((name, dict(args), call_id))
        result = await run_code_job(str(args.get("goal") or ""))
        return json.dumps(
            {
                "ok": result.get("ok"),
                "result": result,
                "spoken": result.get("spoken"),
            }
        )

    class _OpenAI:
        _provider = "openai"
        supports_function_calls = True
        _open_turn_id = "turn-code-kernel"
        _shadow_response_for_turn = None

        async def cancel(self) -> None:
            return None

        async def speak_ack(self, text: str) -> bool:
            spoken.append(text)
            return True

        async def send_text(self, text: str) -> None:
            raise AssertionError(f"Mini must not receive the coding command: {text}")

    grok = _OpenAI()
    live = LiveSession(session_id="owner-code-kernel", backchannel_enabled=False)
    live.run_live_tool = runner
    live.grok_voice = grok
    goal = "write a python script that prints hello world"
    try:
        await _await_s2s(
            live,
            FinalTranscriptEvent(at_ms=1, text=goal, provider="openai-realtime"),
        )
        await _finish_live_code(live)
        assert kernel_calls["n"] == 0
        assert seen == [("code", {"goal": goal}, "owner-code-exec")]
        assert (tmp_path / "hello.py").read_text(encoding="utf-8") == "print('hello world')\n"
        assert spoken
    finally:
        live.close()


@pytest.mark.asyncio
async def test_typed_live_command_runs_code_without_sending_to_mini(
    tmp_path: Path, monkeypatch
) -> None:
    """Typed live text with Mini attached must not be forwarded to S2S."""

    import json

    from app.config import settings
    from app.voice.live.session import LiveSession

    monkeypatch.setattr(settings, "code_workspace", str(tmp_path))
    monkeypatch.setattr(settings, "code_projects_root", "")
    monkeypatch.setattr(settings, "openai_api_key", "")

    seen: list[tuple[str, dict, str]] = []
    spoken: list[str] = []

    async def runner(name: str, args: dict, call_id: str) -> str:
        seen.append((name, dict(args), call_id))
        result = await run_code_job(str(args.get("goal") or ""))
        return json.dumps(
            {
                "ok": result.get("ok"),
                "result": result,
                "spoken": result.get("spoken"),
            }
        )

    class _OpenAI:
        _provider = "openai"
        supports_function_calls = True
        _open_turn_id = "turn-typed-1"
        _shadow_response_for_turn = None
        sent: list[str] = []

        async def cancel(self) -> None:
            return None

        async def speak_ack(self, text: str) -> bool:
            spoken.append(text)
            return True

        async def send_text(self, text: str) -> None:
            self.sent.append(text)

    grok = _OpenAI()
    live = LiveSession(session_id="owner-code-typed", backchannel_enabled=False)
    live.run_live_tool = runner
    live.grok_voice = grok
    goal = "write a python script that prints hello world"
    try:
        await live.handle_client({"type": "text", "text": goal})
        await _finish_live_code(live)
        assert grok.sent == []
        assert seen == [("code", {"goal": goal}, "owner-code-exec")]
        assert (tmp_path / "hello.py").read_text(encoding="utf-8") == "print('hello world')\n"
        assert spoken
        assert grok._shadow_response_for_turn == "turn-typed-1"
    finally:
        live.close()


@pytest.mark.asyncio
async def test_live_s2s_runs_tests_in_named_project(tmp_path: Path, monkeypatch) -> None:
    """Spoken 'in the demo project' must select that root, not the sandbox."""

    import json

    from app.config import settings
    from app.voice.live.events import FinalTranscriptEvent
    from app.voice.live.session import LiveSession

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    demo = _seed_project(tmp_path / "Code" / "demo")
    monkeypatch.setattr(settings, "code_workspace", str(sandbox))
    monkeypatch.setattr(settings, "code_projects_root", str(tmp_path / "Code"))
    monkeypatch.setattr(settings, "openai_api_key", "")

    seen: list[tuple[str, dict, str]] = []
    spoken: list[str] = []
    jobs: list[dict] = []

    async def runner(name: str, args: dict, call_id: str) -> str:
        seen.append((name, dict(args), call_id))
        result = await run_code_job(str(args.get("goal") or ""), actor="voice", channel="voice")
        jobs.append(result)
        return json.dumps({"ok": result.get("ok"), "result": result, "spoken": result.get("spoken")})

    class _OpenAI:
        _provider = "openai"
        supports_function_calls = True
        _open_turn_id = "turn-demo-1"

        async def cancel(self) -> None:
            return None

        async def speak_ack(self, text: str) -> bool:
            spoken.append(text)
            return True

    live = LiveSession(session_id="owner-code-named", backchannel_enabled=False)
    live.run_live_tool = runner
    live.grok_voice = _OpenAI()
    goal = "run the tests in the demo project"
    try:
        await _await_s2s(
            live,
            FinalTranscriptEvent(at_ms=1, text=goal, provider="openai-realtime"),
        )
        await _finish_live_code(live)
        assert seen == [("code", {"goal": goal}, "owner-code-exec")]
        assert jobs and jobs[0].get("ok") is True
        assert jobs[0].get("project") == "demo"
        assert any(item.get("exit_code") == 0 for item in jobs[0].get("runs") or [])
        assert (demo / "mathy.py").is_file()
        assert not (sandbox / "mathy.py").exists()
        assert spoken
    finally:
        live.close()


@pytest.mark.asyncio
async def test_live_tool_runner_dispatches_code_on_voice(
    db_session: AsyncSession, tmp_path: Path, monkeypatch
) -> None:
    """The real live runner must execute `code` with spoken evidence."""

    from app.config import settings
    from app.voice.live.session import LiveSession
    from app.voice.live.transport import _grok_tool_runner

    monkeypatch.setattr(settings, "code_workspace", str(tmp_path))
    monkeypatch.setattr(settings, "code_projects_root", "")
    monkeypatch.setattr(settings, "openai_api_key", "")

    live = LiveSession(session_id="owner-code-dispatch", backchannel_enabled=False)
    runner = _grok_tool_runner(actor="voice", device_id=None, live=live)
    live.run_live_tool = runner
    try:
        pending = __import__("json").loads(
            await runner(
                "code",
                {"goal": "write a python script that prints hello world"},
                "call_mini_code",
            )
        )
        assert pending.get("pending") is True
        await live.drain_code_job()
        assert (tmp_path / "hello.py").read_text(encoding="utf-8") == "print('hello world')\n"
        finished = __import__("json").loads(
            await runner(
                "code",
                {"goal": "write a python script that prints hello world"},
                "owner-code-exec",
            )
        )
        spoken = str(finished.get("spoken") or "")
        assert finished.get("ok") is True
        assert "hello" in spoken.lower() or "hello.py" in spoken
        body = finished.get("result") if isinstance(finished.get("result"), dict) else {}
        changed = body.get("files_changed") or finished.get("files_changed")
        assert changed and "hello.py" in changed
    finally:
        live.close()


@pytest.mark.asyncio
async def test_computer_broker_does_not_type_a_coding_goal(
    db_session: AsyncSession, tmp_path: Path, monkeypatch
) -> None:
    """Mini often calls computer for software. That must still run Luna."""

    from app.config import settings
    from app.ev.computer_strategy import resolve_generic_computer_goal

    monkeypatch.setattr(settings, "code_workspace", str(tmp_path))
    monkeypatch.setattr(settings, "code_projects_root", "")
    monkeypatch.setattr(settings, "openai_api_key", "")
    goal = "write a python script that prints hello world"
    assert resolve_generic_computer_goal(goal) is None
    response = await dispatch(
        db_session,
        "computer",
        {"goal": goal},
        actor="voice",
        allow_sensitive=True,
        channel="voice",
    )
    assert response.ok is True
    body = response.result if isinstance(response.result, dict) else {}
    assert body.get("ok") is True
    assert (tmp_path / "hello.py").read_text(encoding="utf-8") == "print('hello world')\n"
    assert "hello" in str(body.get("spoken") or "").lower() or "hello.py" in str(
        body.get("spoken") or ""
    )


@pytest.mark.asyncio
async def test_capability_router_routes_coding_as_semantic_code(
    db_session: AsyncSession,
) -> None:
    from app.ev.capability_router import RouteKind, goal_from_transcript, route_action

    goal = goal_from_transcript("write a python script that prints hello world")
    assert goal.semantic_intent == "code"
    assert goal.target == "code"
    route = await route_action(goal, session=db_session)
    assert route.route_kind == RouteKind.SEMANTIC
    assert route.capability == "code"


@pytest.mark.asyncio
async def test_gender_script_is_not_a_fake_hello(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings
    from app.ev.luna_code import last_code_job

    monkeypatch.setattr(settings, "code_workspace", str(tmp_path))
    monkeypatch.setattr(settings, "code_projects_root", "")
    monkeypatch.setattr(settings, "openai_api_key", "")
    goal = (
        "create a Python script where if the gender is boy, then the code "
        "should print hello world, and if the gender is female, the code "
        "should print hello miss world"
    )
    result = await run_code_job(goal)
    assert result["ok"] is True
    assert not (tmp_path / "hello.py").exists()
    greet = (tmp_path / "greet.py").read_text(encoding="utf-8")
    assert "hello world" in greet
    assert "hello miss world" in greet
    assert "if " in greet
    spoken = (result["spoken"] or "").lower()
    assert "greet.py" in spoken
    assert "miss" in spoken
    assert "ran" in spoken or "printed" in spoken
    assert looks_like_code_followup("where is the file saved")
    assert not looks_like_code_followup("where is Rahul")
    job = last_code_job()
    assert job is not None
    follow = spoken_code_followup("where is the file saved", job)
    assert "greet.py" in follow.lower()
    assert str(tmp_path.name) in follow or "folder" in follow.lower() or "code" in follow.lower()


@pytest.mark.asyncio
async def test_live_followup_speaks_where_the_file_was_saved(
    tmp_path: Path, monkeypatch
) -> None:
    import json

    from app.config import settings
    from app.voice.live.events import FinalTranscriptEvent
    from app.voice.live.session import LiveSession

    monkeypatch.setattr(settings, "code_workspace", str(tmp_path))
    monkeypatch.setattr(settings, "code_projects_root", "")
    monkeypatch.setattr(settings, "openai_api_key", "")

    seen: list[str] = []
    spoken: list[str] = []

    async def runner(name: str, args: dict, call_id: str) -> str:
        seen.append(name)
        result = await run_code_job(
            str(args.get("goal") or ""),
            actor="voice",
            channel="voice",
            session_key="owner-code-follow",
        )
        return json.dumps(
            {"ok": result.get("ok"), "result": result, "spoken": result.get("spoken")}
        )

    class _OpenAI:
        _provider = "openai"
        supports_function_calls = True
        _open_turn_id = "turn-follow-1"

        async def cancel(self) -> None:
            return None

        async def speak_ack(self, text: str) -> bool:
            spoken.append(text)
            return True

        async def speak_life_record(self, text: str) -> bool:
            spoken.append(text)
            return True

    live = LiveSession(session_id="owner-code-follow", backchannel_enabled=False)
    live.run_live_tool = runner
    live.grok_voice = _OpenAI()
    goal = (
        "create a Python script where if the gender is boy print hello world "
        "and if the gender is female print hello miss world"
    )
    try:
        await _await_s2s(
            live,
            FinalTranscriptEvent(at_ms=1, text=goal, provider="openai-realtime"),
        )
        await _finish_live_code(live)
        assert seen == ["code"]
        assert (tmp_path / "greet.py").is_file()
        assert spoken
        assert "greet.py" in spoken[0].lower()
        spoken.clear()
        live.grok_voice._open_turn_id = "turn-follow-2"
        await _await_s2s(
            live,
            FinalTranscriptEvent(
                at_ms=2, text="where is the file saved", provider="openai-realtime"
            ),
        )
        assert seen == ["code"]
        assert spoken
        assert "greet.py" in spoken[0].lower()
    finally:
        live.close()


@pytest.mark.asyncio
async def test_live_code_job_keeps_the_mouth_free(tmp_path: Path, monkeypatch) -> None:
    """A slow Luna job must not block emit; the receipt still lands after."""

    import json
    import time

    from app.config import settings
    from app.voice.live.events import FinalTranscriptEvent
    from app.voice.live.session import LiveSession

    monkeypatch.setattr(settings, "code_workspace", str(tmp_path))
    monkeypatch.setattr(settings, "code_projects_root", "")
    monkeypatch.setattr(settings, "openai_api_key", "")

    spoken: list[str] = []
    started = {"at": 0.0}

    async def runner(name: str, args: dict, call_id: str) -> str:
        import asyncio

        await asyncio.sleep(2.0)
        result = await run_code_job(str(args.get("goal") or ""), actor="voice", channel="voice")
        return json.dumps(
            {"ok": result.get("ok"), "result": result, "spoken": result.get("spoken")}
        )

    class _OpenAI:
        _provider = "openai"
        supports_function_calls = True
        _open_turn_id = "turn-slow-1"

        async def cancel(self) -> None:
            return None

        async def speak_ack(self, text: str) -> bool:
            spoken.append(text)
            return True

        async def speak_life_record(self, text: str) -> bool:
            spoken.append(text)
            return True

    live = LiveSession(session_id="owner-code-slow", backchannel_enabled=False)
    live.run_live_tool = runner
    live.grok_voice = _OpenAI()
    goal = "write a python script that prints hello world"
    try:
        started["at"] = time.monotonic()
        await _await_s2s(
            live,
            FinalTranscriptEvent(at_ms=1, text=goal, provider="openai-realtime"),
        )
        assert time.monotonic() - started["at"] < 0.8
        await _finish_live_code(live)
        assert (tmp_path / "hello.py").is_file()
        assert any("hello" in item.lower() for item in spoken)
    finally:
        live.close()


@pytest.mark.asyncio
async def test_live_info_ask_does_not_claim_a_background_write(
    tmp_path: Path, monkeypatch
) -> None:
    import asyncio
    import json
    import time

    from app.voice.live.events import FinalTranscriptEvent
    from app.voice.live.session import LiveSession

    _literacy_env(tmp_path, monkeypatch)
    spoken: list[str] = []

    async def runner(name: str, args: dict, call_id: str) -> str:
        await asyncio.sleep(2.0)
        result = await run_code_job(str(args.get("goal") or ""), actor="voice", channel="voice")
        return json.dumps(
            {"ok": result.get("ok"), "result": result, "spoken": result.get("spoken")}
        )

    class _OpenAI:
        _provider = "openai"
        supports_function_calls = True
        _open_turn_id = "turn-info-1"

        async def cancel(self) -> None:
            return None

        async def speak_ack(self, text: str) -> bool:
            spoken.append(text)
            return True

        async def speak_life_record(self, text: str) -> bool:
            spoken.append(text)
            return True

    live = LiveSession(session_id="owner-code-info", backchannel_enabled=False)
    live.run_live_tool = runner
    live.grok_voice = _OpenAI()
    ask = "give me info and some details about the wish project"
    try:
        started = time.monotonic()
        await _await_s2s(
            live,
            FinalTranscriptEvent(at_ms=1, text=ask, provider="openai-realtime"),
        )
        assert time.monotonic() - started < 0.8
        await asyncio.sleep(1.4)
        joined = " ".join(spoken).lower()
        assert "background" not in joined
        assert "i'll tell you when it's saved" not in joined
        assert "writing that" not in joined
        await _finish_live_code(live)
        done = " ".join(spoken).lower()
        assert "background" not in done
        assert "writing that" not in done
        assert "wish" in done or "sweet potato" in done
    finally:
        live.close()


@pytest.mark.asyncio
async def test_owner_inspect_pending_json_is_not_a_write_claim(
    tmp_path: Path, monkeypatch
) -> None:
    import json

    from app.voice.live.session import LiveSession

    _literacy_env(tmp_path, monkeypatch)

    async def runner(name: str, args: dict, call_id: str) -> str:
        await asyncio.sleep(0.4)
        result = await run_code_job(str(args.get("goal") or ""), actor="voice", channel="voice")
        return json.dumps(
            {"ok": result.get("ok"), "result": result, "spoken": result.get("spoken")}
        )

    live = LiveSession(session_id="owner-inspect-pending", backchannel_enabled=False)
    live.run_live_tool = runner
    ask = "give me info and some details about the wish project"
    try:
        raw = await live.begin_background_code_job({"goal": ask}, "owner-code")
        payload = json.loads(raw)
        spoken = str(payload.get("spoken") or "").lower()
        assert payload.get("pending") is True
        assert "writing that" not in spoken
        assert "i'll tell you when it's saved" not in spoken
        await _finish_live_code(live)
    finally:
        live.close()


@pytest.mark.asyncio
async def test_mini_inspect_job_returns_spoken_answer_not_write_filler(
    tmp_path: Path, monkeypatch
) -> None:
    import json

    from app.voice.live.session import LiveSession

    _literacy_env(tmp_path, monkeypatch)

    async def runner(name: str, args: dict, call_id: str) -> str:
        result = await run_code_job(str(args.get("goal") or ""), actor="voice", channel="voice")
        return json.dumps(
            {"ok": result.get("ok"), "result": result, "spoken": result.get("spoken")}
        )

    live = LiveSession(session_id="mini-inspect", backchannel_enabled=False)
    live.run_live_tool = runner
    ask = "give me info and some details about the wish project"
    try:
        raw = await live.begin_background_code_job({"goal": ask}, "call_mini_inspect")
        payload = json.loads(raw)
        spoken = str(payload.get("spoken") or "").lower()
        assert payload.get("pending") is False
        assert payload.get("must_continue") is False
        assert "writing that" not in spoken
        assert "i'll tell you when it's saved" not in spoken
        assert "wish" in spoken or "sweet potato" in spoken
        assert live._code_job_busy() is False
    finally:
        live.close()


@pytest.mark.asyncio
async def test_luna_multi_file_edit_in_named_project(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    demo = _seed_project(tmp_path / "Code" / "demo")
    (demo / "util.py").write_text("def label():\n    return 'old'\n", encoding="utf-8")
    monkeypatch.setattr(settings, "code_workspace", str(sandbox))
    monkeypatch.setattr(settings, "code_projects_root", str(tmp_path / "Code"))
    monkeypatch.setattr(settings, "openai_api_key", "sk-test-luna")
    monkeypatch.setattr(settings, "code_model", "gpt-5.6-luna")

    class _Resp:
        def __init__(self, payload: dict, status: int = 200) -> None:
            self.status_code = status
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    class _FakeClient:
        posts = 0

        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def post(self, url, headers=None, json=None):
            _FakeClient.posts += 1
            if _FakeClient.posts == 1:
                return _Resp(
                    {
                        "output": [
                            {
                                "type": "function_call",
                                "call_id": "call_search",
                                "name": "search",
                                "arguments": '{"pattern":"def label"}',
                            }
                        ]
                    }
                )
            if _FakeClient.posts == 2:
                return _Resp(
                    {
                        "output": [
                            {
                                "type": "function_call",
                                "call_id": "call_patch",
                                "name": "replace_in_file",
                                "arguments": (
                                    '{"path":"util.py","old":"return \'old\'",'
                                    '"new":"return \'new\'"}'
                                ),
                            }
                        ]
                    }
                )
            if _FakeClient.posts == 3:
                return _Resp(
                    {
                        "output": [
                            {
                                "type": "function_call",
                                "call_id": "call_write",
                                "name": "write_file",
                                "arguments": (
                                    '{"path":"test_util.py","content":'
                                    '"from util import label\\n\\ndef test_label():\\n'
                                    "    assert label() == 'new'\\n\"}"
                                ),
                            }
                        ]
                    }
                )
            if _FakeClient.posts == 4:
                return _Resp(
                    {
                        "output": [
                            {
                                "type": "function_call",
                                "call_id": "call_run",
                                "name": "run_command",
                                "arguments": '{"argv":["python3","-m","pytest","-q"]}',
                            }
                        ]
                    }
                )
            return _Resp(
                {
                    "output_text": (
                        "I patched util.py and added test_util.py in demo. Tests passed."
                    ),
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": (
                                        "I patched util.py and added test_util.py in demo. "
                                        "Tests passed."
                                    ),
                                }
                            ],
                        }
                    ],
                }
            )

    monkeypatch.setattr("httpx.AsyncClient", _FakeClient)
    result = await run_code_job(
        "in the demo project, change label to return new and add a test",
        actor="master",
        channel="action",
    )
    assert result["ok"] is True
    assert result.get("project") == "demo"
    assert (demo / "util.py").read_text(encoding="utf-8").count("new")
    assert (demo / "test_util.py").is_file()
    assert not (sandbox / "util.py").exists()
    changed = result.get("files_changed") or []
    assert "util.py" in changed
    assert "test_util.py" in changed
    assert any(item.get("ok") for item in result.get("runs") or [])
    spoken = (result.get("spoken") or "").lower()
    assert "util.py" in spoken or "demo" in spoken


@pytest.mark.asyncio
async def test_luna_live_budget_allows_long_jobs(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "code_workspace", str(tmp_path))
    monkeypatch.setattr(settings, "code_projects_root", "")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test-luna")
    monkeypatch.setattr(settings, "code_max_steps", 24)
    monkeypatch.setattr(settings, "code_live_job_seconds", 60.0)

    class _Resp:
        def __init__(self, payload: dict, status: int = 200) -> None:
            self.status_code = status
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    class _FakeClient:
        posts = 0

        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def post(self, url, headers=None, json=None):
            _FakeClient.posts += 1
            return _Resp(
                {
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": f"call_{_FakeClient.posts}",
                            "name": "list_dir",
                            "arguments": "{}",
                        }
                    ]
                }
            )

    monkeypatch.setattr("httpx.AsyncClient", _FakeClient)
    await run_code_job(
        "refactor the auth module into a state machine",
        actor="voice",
        channel="voice",
    )
    assert _FakeClient.posts == 48


@pytest.mark.asyncio
async def test_luna_live_hello_stays_on_the_short_budget(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "code_workspace", str(tmp_path))
    monkeypatch.setattr(settings, "code_projects_root", "")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test-luna")
    monkeypatch.setattr(settings, "code_max_steps", 24)
    monkeypatch.setattr(settings, "code_live_job_seconds", 60.0)

    class _Resp:
        def __init__(self, payload: dict, status: int = 200) -> None:
            self.status_code = status
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    class _FakeClient:
        posts = 0

        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def post(self, url, headers=None, json=None):
            _FakeClient.posts += 1
            return _Resp(
                {
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": f"call_{_FakeClient.posts}",
                            "name": "list_dir",
                            "arguments": "{}",
                        }
                    ]
                }
            )

    monkeypatch.setattr("httpx.AsyncClient", _FakeClient)
    await run_code_job(
        "write a python script that prints hello world",
        actor="voice",
        channel="voice",
    )
    assert _FakeClient.posts == 20


@pytest.mark.asyncio
async def test_heuristic_javascript_hello(tmp_path: Path, monkeypatch) -> None:
    import shutil

    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    from app.config import settings

    monkeypatch.setattr(settings, "code_workspace", str(tmp_path))
    monkeypatch.setattr(settings, "code_projects_root", "")
    monkeypatch.setattr(settings, "openai_api_key", "")
    result = await run_code_job("write a javascript script that prints hello world")
    assert result["ok"] is True
    assert (tmp_path / "hello.js").read_text(encoding="utf-8") == "console.log('hello world')\n"
    assert not (tmp_path / "hello.py").exists()
    assert any(item.get("exit_code") == 0 for item in result["runs"])


@pytest.mark.asyncio
async def test_run_it_continues_the_last_job(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "code_workspace", str(tmp_path))
    monkeypatch.setattr(settings, "code_projects_root", "")
    monkeypatch.setattr(settings, "openai_api_key", "")
    first = await run_code_job(
        "write a python script that prints hello world",
        session_key="flex-run",
    )
    assert first["ok"] is True
    result = await run_code_job("run it", session_key="flex-run")
    assert result["ok"] is True
    assert any(item.get("exit_code") == 0 for item in result["runs"])
    spoken = (result.get("spoken") or "").lower()
    assert "hello" in spoken or "ran" in spoken


@pytest.mark.asyncio
async def test_change_threshold_patches_last_files(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings
    from app.ev.code_runtime import set_active_project
    from app.ev.code_runtime import write_file as jail_write

    monkeypatch.setattr(settings, "code_workspace", str(tmp_path))
    monkeypatch.setattr(settings, "code_projects_root", "")
    monkeypatch.setattr(settings, "openai_api_key", "")
    token = set_active_project(tmp_path)
    try:
        jail_write(
            "score.py",
            "def grade(n):\n    return 'pass' if n >= 50 else 'fail'\n\n"
            "if __name__ == '__main__':\n    print(grade(50))\n",
        )
    finally:
        from app.ev.code_runtime import reset_active_project

        reset_active_project(token)
    remember_code_job(
        {
            "ok": True,
            "workspace": str(tmp_path),
            "project": tmp_path.name,
            "files_changed": ["score.py"],
            "spoken": "Wrote score.py",
            "goal": "make a grader",
            "runs": [],
        },
        session_key="flex-change",
    )
    result = await run_code_job("change 50 to 60", session_key="flex-change")
    assert result["ok"] is True
    body = (tmp_path / "score.py").read_text(encoding="utf-8")
    assert ">= 60" in body
    assert ">= 50" not in body


@pytest.mark.asyncio
async def test_live_run_it_after_a_script(tmp_path: Path, monkeypatch) -> None:
    import json

    from app.config import settings
    from app.voice.live.events import FinalTranscriptEvent
    from app.voice.live.session import LiveSession

    monkeypatch.setattr(settings, "code_workspace", str(tmp_path))
    monkeypatch.setattr(settings, "code_projects_root", "")
    monkeypatch.setattr(settings, "openai_api_key", "")

    spoken: list[str] = []

    async def runner(name: str, args: dict, call_id: str) -> str:
        result = await run_code_job(
            str(args.get("goal") or ""),
            actor="voice",
            channel="voice",
            session_key="owner-code-flex",
        )
        return json.dumps(
            {"ok": result.get("ok"), "result": result, "spoken": result.get("spoken")}
        )

    class _OpenAI:
        _provider = "openai"
        supports_function_calls = True
        _open_turn_id = "turn-flex-1"

        async def cancel(self) -> None:
            return None

        async def speak_ack(self, text: str) -> bool:
            spoken.append(text)
            return True

        async def speak_life_record(self, text: str) -> bool:
            spoken.append(text)
            return True

    live = LiveSession(session_id="owner-code-flex", backchannel_enabled=False)
    live.run_live_tool = runner
    live.grok_voice = _OpenAI()
    try:
        await _await_s2s(
            live,
            FinalTranscriptEvent(
                at_ms=1,
                text="write a python script that prints hello world",
                provider="openai-realtime",
            ),
        )
        await _finish_live_code(live)
        spoken.clear()
        live.grok_voice._open_turn_id = "turn-flex-2"
        await _await_s2s(
            live,
            FinalTranscriptEvent(at_ms=2, text="run it", provider="openai-realtime"),
        )
        await _finish_live_code(live)
        assert spoken
        assert any("ran" in item.lower() or "hello" in item.lower() for item in spoken)
        assert (tmp_path / "hello.py").is_file()
    finally:
        live.close()


@pytest.mark.asyncio
async def test_chat_run_it_continues_last_job(
    db_session: AsyncSession, tmp_path: Path, monkeypatch
) -> None:
    from app.config import settings
    from app.ev.turn import execute_requested_actions

    monkeypatch.setattr(settings, "code_workspace", str(tmp_path))
    monkeypatch.setattr(settings, "openai_api_key", "")
    first = await execute_requested_actions(
        db_session,
        "write a python script that prints hello world",
        actor="master",
        allow_sensitive=True,
    )
    assert first and first[0].ok is True
    receipts = await execute_requested_actions(
        db_session,
        "run it",
        actor="master",
        allow_sensitive=True,
    )
    assert receipts
    assert receipts[0].name == "code"
    assert receipts[0].ok is True


class _ScriptedSparkProvider:
    """Mimics the Meta Responses projection `complete_raw` returns to loops."""

    def __init__(self, replies: list) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict]] = []
        self.tool_choices: list = []

    async def complete_raw(self, messages, *, tools=None, model=None, response_format=None, tool_choice=None):
        self.calls.append([dict(message) for message in messages])
        self.tool_choices.append(tool_choice)
        if not self.replies:
            return {"choices": [{"message": {"role": "assistant", "content": "Done."}}]}
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _spark_tool_reply(call_id: str, name: str, arguments: dict) -> dict:
    import json

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


def _spark_text_reply(text: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def _force_spark_path(monkeypatch, tmp_path, model: str = "muse-spark-1.3-contributor"):
    from app.config import settings

    monkeypatch.setattr(settings, "code_workspace", str(tmp_path))
    monkeypatch.setattr(settings, "code_projects", "")
    monkeypatch.setattr(settings, "code_projects_root", "")
    monkeypatch.setattr(settings, "code_max_steps", 8)
    monkeypatch.setattr("app.gateway.muse.muse_brain_active", lambda: True)
    monkeypatch.setattr("app.gateway.muse.muse_spark_key_loaded", lambda: True)
    monkeypatch.setattr("app.gateway.muse.muse_spark_model", lambda: model)
    return model


@pytest.mark.asyncio
async def test_spark_code_loop_runs_real_jail_tools_end_to_end(
    tmp_path: Path, monkeypatch
) -> None:
    _force_spark_path(monkeypatch, tmp_path)
    provider = _ScriptedSparkProvider(
        [
            _spark_tool_reply(
                "c1",
                "write_file",
                {"path": "mod.py", "content": "def add(a, b):\n    return a + b\n"},
            ),
            _spark_tool_reply(
                "c2",
                "write_file",
                {
                    "path": "test_mod.py",
                    "content": "from mod import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
                },
            ),
            _spark_tool_reply("c3", "run_command", {"argv": ["python3", "mod.py"]}),
            _spark_text_reply("Wrote mod.py and test_mod.py; the script ran clean."),
        ]
    )
    monkeypatch.setattr("app.gateway.muse_spark.muse_spark_provider", lambda: provider)

    result = await run_code_job("add an add() helper with a test")

    assert result["ok"] is True
    assert result["partial"] is False
    assert result["timed_out"] is False
    assert result["error"] is None
    assert result["files_changed"] == ["mod.py", "test_mod.py"]
    assert (tmp_path / "mod.py").read_text(encoding="utf-8").startswith("def add")
    assert (tmp_path / "test_mod.py").exists()
    assert any(item.get("exit_code") == 0 for item in result["runs"])
    # The second model call must carry the first tool round back in a shape
    # the Responses adapter converts to function_call_output.
    second = provider.calls[1]
    assert any(str(message.get("role")) == "tool" for message in second)
    assert any(str(message.get("role")) == "assistant" and message.get("tool_calls") for message in second)


@pytest.mark.asyncio
async def test_spark_code_loop_keeps_partial_files_when_provider_fails(
    tmp_path: Path, monkeypatch
) -> None:
    _force_spark_path(monkeypatch, tmp_path)
    provider = _ScriptedSparkProvider(
        [
            _spark_tool_reply(
                "c1",
                "write_file",
                {"path": "partial.py", "content": "print('hi')\n"},
            ),
            RuntimeError("meta 500"),
        ]
    )
    monkeypatch.setattr("app.gateway.muse_spark.muse_spark_provider", lambda: provider)

    result = await run_code_job("write partial.py then verify it")

    assert result["ok"] is False
    assert result["partial"] is True
    assert result["timed_out"] is False
    assert result["error"] == "RuntimeError"
    assert result["files_changed"] == ["partial.py"]
    assert (tmp_path / "partial.py").exists()
    spoken = str(result["spoken"]).lower()
    assert "partial.py" in spoken
    assert "verify" in spoken or "problem" in spoken


@pytest.mark.asyncio
async def test_spark_code_loop_times_out_honestly(tmp_path: Path, monkeypatch) -> None:
    import asyncio

    from app.ev import luna_code

    _force_spark_path(monkeypatch, tmp_path)

    class _SlowProvider:
        async def complete_raw(self, messages, **kwargs):
            await asyncio.sleep(3)

    monkeypatch.setattr("app.gateway.muse_spark.muse_spark_provider", lambda: _SlowProvider())

    result = await luna_code._spark_code_loop(
        "write slow.py",
        model="muse-spark-1.3-contributor",
        budget_s=0.1,
        live=False,
    )

    assert result["timed_out"] is True
    assert result["partial"] is True
    assert result["ok"] is False
    assert result["files_changed"] == []


def test_tool_output_clip_keeps_failure_tail() -> None:
    from app.ev.luna_code import _clip_tool_output

    payload = {"exit_code": 1, "stdout": "x" * 20_000 + "\nFAILED test_tail_marker"}
    text = _clip_tool_output(payload)

    assert "FAILED test_tail_marker" in text
    assert "chars trimmed" in text
    assert len(text) < 16_500


def test_loop_compaction_bounds_context() -> None:
    from app.ev.luna_code import _compact_loop, _loop_chars

    messages: list[dict] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "go"},
    ]
    for index in range(40):
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"c{index}",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            }
        )
        messages.append({"role": "tool", "tool_call_id": f"c{index}", "content": "y" * 20_000})

    before = _loop_chars(messages)
    _compact_loop(messages)
    after = _loop_chars(messages)

    assert before > 140_000
    assert after < before
    assert messages[0]["content"] == "sys"
    assert messages[1]["content"] == "go"
    # Index 2 is the trim note; the first kept history row must be an
    # assistant call, never an orphaned tool row.
    assert str(messages[2].get("role")) == "user"
    assert "trimmed" in str(messages[2].get("content"))
    assert str(messages[3].get("role")) == "assistant"
    assert after <= 150_000


def test_kernel_deadline_caps_code_job_budget() -> None:
    import time

    from app.ev.luna_code import (
        _effective_job_budget,
        kernel_turn_budget,
        kernel_turn_remaining_s,
        reset_kernel_turn_deadline,
        set_kernel_turn_deadline,
    )

    assert _effective_job_budget(300) == 300.0
    token = set_kernel_turn_deadline(time.monotonic() + 30.0)
    try:
        remaining = kernel_turn_remaining_s()
        assert remaining is not None and 0 < remaining <= 30.0
        capped = _effective_job_budget(300)
        assert 5.0 <= capped <= 28.5
    finally:
        reset_kernel_turn_deadline(token)
    assert _effective_job_budget(300) == 300.0
    with kernel_turn_budget(None):
        assert _effective_job_budget(60) == 60.0


def test_fresh_deliverable_does_not_continue_prior_job() -> None:
    from app.ev.luna_code import _continues_prior_job

    assert _continues_prior_job("run it") is True
    assert _continues_prior_job("run the tests") is True
    assert _continues_prior_job("add a test to it") is True
    assert _continues_prior_job("also add a test") is True
    assert _continues_prior_job("do that in python instead") is True
    assert _continues_prior_job("write a python module stats.py and run the tests") is False
    assert _continues_prior_job("in the ev repo add a test for mean") is False
    assert _continues_prior_job("write hello world") is False


def _seed_stale_prior(luna_code, old: Path) -> None:
    luna_code.remember_code_job(
        {"workspace": str(old), "files": ["old.py"], "goal": "write old.py", "ok": True},
        session_key="owner",
    )


@pytest.mark.asyncio
async def test_fresh_request_stays_in_default_workspace_with_stale_prior(
    tmp_path: Path, monkeypatch
) -> None:
    from app.config import settings
    from app.ev import luna_code

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    old = tmp_path / "oldproject"
    old.mkdir()
    monkeypatch.setattr(settings, "code_workspace", str(sandbox))
    monkeypatch.setattr(settings, "code_projects", "")
    monkeypatch.setattr(settings, "code_projects_root", "")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(luna_code, "_persist_last_code_job", lambda payload: None)
    monkeypatch.setattr(luna_code, "_load_last_code_job", lambda: None)
    monkeypatch.setattr(luna_code, "_LAST_CODE_JOBS", {})
    _seed_stale_prior(luna_code, old)

    result = await run_code_job("write a python module stats.py and run the tests")

    assert str(sandbox) in str(result.get("workspace") or "")
    assert str(old) not in str(result.get("workspace") or "")


@pytest.mark.asyncio
async def test_pure_continuation_keeps_prior_workspace(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings
    from app.ev import luna_code

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    old = tmp_path / "oldproject"
    old.mkdir()
    (old / "old.py").write_text("value = 50\n", encoding="utf-8")
    monkeypatch.setattr(settings, "code_workspace", str(sandbox))
    monkeypatch.setattr(settings, "code_projects", "")
    monkeypatch.setattr(settings, "code_projects_root", "")
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(luna_code, "_persist_last_code_job", lambda payload: None)
    monkeypatch.setattr(luna_code, "_load_last_code_job", lambda: None)
    monkeypatch.setattr(luna_code, "_LAST_CODE_JOBS", {})
    _seed_stale_prior(luna_code, old)

    result = await run_code_job("change 50 to 60")

    assert str(old) in str(result.get("workspace") or "")
    assert "60" in (old / "old.py").read_text(encoding="utf-8")


def test_shape_code_spoken_does_not_repeat_a_fake_success() -> None:
    from app.ev.luna_code import shape_code_spoken
    from app.ev.tools import life_success_reply

    spoken = shape_code_spoken(
        {
            "ok": False,
            "spoken": "I wrote hello.py and ran it.",
            "files_changed": [],
            "workspace": "/tmp/ws",
        }
    )
    assert "wrote" not in spoken.lower()
    assert "couldn't finish" in spoken.lower()
    reply = life_success_reply(
        {
            "ok": False,
            "degraded": True,
            "spoken": "I wrote hello.py and ran it.",
            "files_changed": [],
        },
        tool_name="code",
    )
    assert "wrote" not in reply.lower()
    assert "couldn't finish" in reply.lower()
    lying_ok = life_success_reply(
        {
            "ok": True,
            "spoken": "I wrote hello.py and ran it.",
            "files_changed": [],
        },
        tool_name="code",
    )
    assert "wrote" not in lying_ok.lower()
    assert "couldn't finish" in lying_ok.lower()


@pytest.mark.asyncio
async def test_complete_raw_projects_output_tools_over_lying_choices() -> None:
    from app.gateway.muse_spark import MuseSparkProvider

    provider = MuseSparkProvider(
        base_url="https://api.meta.ai/v1",
        api_key="test-key",
        default_model="muse-spark-1.3-contributor",
    )

    async def fake_post(_payload):
        return {
            "output": [
                {
                    "type": "function_call",
                    "call_id": "c1",
                    "name": "write_file",
                    "arguments": '{"path":"hello.py","content":"print(1)\\n"}',
                }
            ],
            "choices": [
                {"message": {"role": "assistant", "content": "I already wrote hello.py"}}
            ],
            "output_text": "I already wrote hello.py",
        }

    provider._post_json = fake_post  # type: ignore[method-assign]
    data = await provider.complete_raw(
        [{"role": "user", "content": "write hello.py"}],
        tools=[{"type": "function", "name": "write_file", "parameters": {}}],
    )
    calls = ((data.get("choices") or [{}])[0].get("message") or {}).get("tool_calls") or []
    assert calls
    assert calls[0]["function"]["name"] == "write_file"


@pytest.mark.asyncio
async def test_spark_code_loop_nudges_when_model_only_talks(
    tmp_path: Path, monkeypatch
) -> None:
    _force_spark_path(monkeypatch, tmp_path)
    provider = _ScriptedSparkProvider(
        [
            _spark_text_reply("I wrote hello.py and ran it."),
            _spark_tool_reply(
                "c1",
                "write_file",
                {"path": "hello.py", "content": "print('hi')\n"},
            ),
            _spark_tool_reply("c2", "run_command", {"argv": ["python3", "hello.py"]}),
            _spark_text_reply("Saved hello.py."),
        ]
    )
    monkeypatch.setattr("app.gateway.muse_spark.muse_spark_provider", lambda: provider)
    result = await run_code_job("write a python script that prints hi")
    assert (tmp_path / "hello.py").exists()
    assert result["ok"] is True
    assert "hello.py" in result["files_changed"]
    assert provider.tool_choices
    assert all(choice in {None, "auto"} for choice in provider.tool_choices)
    second = provider.calls[1]
    assert any(
        "instead of doing it" in str(message.get("content") or "") for message in second
    )


@pytest.mark.asyncio
async def test_spark_talk_without_tools_still_writes_via_heuristic(
    tmp_path: Path, monkeypatch
) -> None:
    _force_spark_path(monkeypatch, tmp_path)
    provider = _ScriptedSparkProvider(
        [
            _spark_text_reply("I wrote hello.py already."),
            _spark_text_reply("Still done."),
        ]
    )
    monkeypatch.setattr("app.gateway.muse_spark.muse_spark_provider", lambda: provider)
    result = await run_code_job("write a python script that prints hello world")
    assert (tmp_path / "hello.py").exists()
    assert result["ok"] is True
    assert "heuristic" in str(result.get("brain") or "")
    spoken = str(result.get("spoken") or "").lower()
    assert "hello.py" in spoken or "saved" in spoken


@pytest.mark.asyncio
async def test_spark_git_status_alone_is_not_a_successful_write(
    tmp_path: Path, monkeypatch
) -> None:
    from app.ev import luna_code

    _force_spark_path(monkeypatch, tmp_path)
    provider = _ScriptedSparkProvider(
        [
            _spark_tool_reply("c1", "run_command", {"argv": ["git", "status", "--short"]}),
            _spark_text_reply("I wrote the calculator and ran it."),
        ]
    )
    monkeypatch.setattr("app.gateway.muse_spark.muse_spark_provider", lambda: provider)
    result = await luna_code._spark_code_loop(
        "create a calculator app UI",
        model="muse-spark-1.3-contributor",
        budget_s=30,
        live=False,
    )
    assert result["ok"] is False
    assert result["files_changed"] == []
    spoken = str(result.get("spoken") or "").lower()
    assert "wrote" not in spoken
    assert "couldn't" in spoken or "verified" in spoken


def test_studio_brief_does_not_claim_shipped_without_files() -> None:
    from app.ev.code_studio import spoken_completion_summary

    spoken = spoken_completion_summary(
        {
            "title": "calculator UI",
            "folder": "calc",
            "files": [],
            "phases": [{"title": "Foundation", "status": "skipped"}],
        },
        ok=True,
    )
    lowered = spoken.lower()
    assert "shipped" not in lowered
    assert "don't have files" in lowered or "do not have files" in lowered


def test_sticky_project_survives_vague_repo_phrasing(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings
    from app.ev.code_runtime import (
        clear_sticky_project,
        select_project,
        use_project,
    )
    from app.ev.luna_code import maybe_switch_coding_project

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    code_home = tmp_path / "Code"
    ev_root = _seed_project(code_home / "ev")
    monkeypatch.setattr(settings, "code_workspace", str(sandbox))
    monkeypatch.setattr(settings, "code_projects_root", str(code_home))
    clear_sticky_project()
    assert select_project("write a python script that prints hello world") == sandbox.resolve()
    spoken = maybe_switch_coding_project("use the ev repo")
    assert spoken is not None and "ev" in spoken.lower()
    assert use_project("ev")["ok"] is True
    assert select_project("refactor the auth module") == ev_root.resolve()
    assert select_project("fix that in my repo") == ev_root.resolve()
    assert select_project("write a python script that prints hello world") == sandbox.resolve()
    clear_sticky_project()
    assert select_project("refactor the auth module") == sandbox.resolve()


def test_git_checkout_branch_is_allowlisted(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "code_workspace", str(tmp_path))
    denied = execute_code_tool("run_command", {"argv": ["git", "push"]})
    assert denied["ok"] is False
    whole = execute_code_tool("run_command", {"argv": ["git", "checkout", "."]})
    assert whole["ok"] is False
    forced = execute_code_tool("run_command", {"argv": ["git", "checkout", "-f", "main"]})
    assert forced["ok"] is False
    # Not a git repo — jail allows the subcommand; git itself fails.
    ran = execute_code_tool("run_command", {"argv": ["git", "checkout", "-b", "evie/feature"]})
    assert ran.get("error") != "unknown_code_tool"
    assert "not allowlisted" not in str(ran.get("detail") or "").lower()


@pytest.mark.asyncio
async def test_spark_does_not_heuristic_hello_into_a_named_repo(
    tmp_path: Path, monkeypatch
) -> None:
    from app.config import settings
    from app.ev.code_runtime import clear_sticky_project

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    demo = _seed_project(tmp_path / "Code" / "demo")
    monkeypatch.setattr(settings, "code_workspace", str(sandbox))
    monkeypatch.setattr(settings, "code_projects_root", str(tmp_path / "Code"))
    clear_sticky_project()
    _force_spark_path(monkeypatch, sandbox)
    monkeypatch.setattr(settings, "code_workspace", str(sandbox))
    monkeypatch.setattr(settings, "code_projects_root", str(tmp_path / "Code"))
    provider = _ScriptedSparkProvider(
        [
            _spark_text_reply("I refactored auth already."),
            _spark_text_reply("Still done."),
        ]
    )
    monkeypatch.setattr("app.gateway.muse_spark.muse_spark_provider", lambda: provider)
    result = await run_code_job("refactor the auth module in the demo repo")
    assert result["ok"] is False
    assert not (demo / "hello.py").exists()
    assert "heuristic" not in str(result.get("brain") or "")
    spoken = str(result.get("spoken") or "").lower()
    assert "wrote" not in spoken
    assert "couldn't" in spoken or "unavailable" in spoken or "verified" in spoken


@pytest.mark.asyncio
async def test_spark_keeps_going_after_a_failed_check(tmp_path: Path, monkeypatch) -> None:
    _force_spark_path(monkeypatch, tmp_path)
    (tmp_path / "mod.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (tmp_path / "test_mod.py").write_text(
        "from mod import add\n\n"
        "ok = add(2, 3) == 5\n"
        "raise SystemExit(0 if ok else 1)\n",
        encoding="utf-8",
    )
    provider = _ScriptedSparkProvider(
        [
            _spark_tool_reply(
                "c1",
                "replace_in_file",
                {"path": "mod.py", "old": "return a - b", "new": "return a - b"},
            ),
            _spark_tool_reply("c2", "run_command", {"argv": ["python3", "test_mod.py"]}),
            _spark_text_reply("Tests passed."),
            _spark_tool_reply(
                "c3",
                "replace_in_file",
                {"path": "mod.py", "old": "return a - b", "new": "return a + b"},
            ),
            _spark_tool_reply("c4", "run_command", {"argv": ["python3", "test_mod.py"]}),
            _spark_text_reply("Fixed the test."),
        ]
    )
    monkeypatch.setattr("app.gateway.muse_spark.muse_spark_provider", lambda: provider)
    result = await run_code_job("fix the failing test in mod.py")
    joined = " ".join(
        str(message.get("content") or "") for batch in provider.calls for message in batch
    )
    assert "last command failed" in joined.lower()
    assert "mod.py" in result["files_changed"]
    assert (tmp_path / "mod.py").read_text(encoding="utf-8").count("return a + b")


def _seed_wish_project(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir()
    (root / "OVERVIEW.md").write_text(
        "# Wish — Sweet Potato — Project Overview\n\n"
        "Inspected from /tmp/wish on 2026-09-13.\n\n"
        "## What it is\n"
        "`wish` is **Sweet Potato**, a Next.js Three.js birthday film for the owner.\n\n"
        "## Purpose\n"
        "Personal cinematic gift: interactive birthday film.\n",
        encoding="utf-8",
    )
    (root / "package.json").write_text(
        '{"name":"wish","description":"Interactive Three.js workspace",'
        '"dependencies":{"next":"16.3.4","three":"0.186.0"},'
        '"scripts":{"dev":"next dev","test":"npm run lint"}}\n',
        encoding="utf-8",
    )
    (root / "src").mkdir()
    (root / "src" / "app").mkdir()
    (root / "src" / "app" / "layout.tsx").write_text(
        'export const metadata = { title: "Sweet Potato", '
        'description: "A little visitor is waiting for you." };\n',
        encoding="utf-8",
    )
    (root / "src" / "experience").mkdir()
    (root / "src" / "experience" / "PlushTeddy.tsx").write_text(
        "export function PlushTeddy() { return null }\n",
        encoding="utf-8",
    )
    return root


def test_wish_workspace_is_a_named_code_project(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings
    from app.ev.code_runtime import clear_sticky_project, select_project
    from app.ev.code_studio import looks_like_long_code_goal, maybe_handle_code_ops
    from app.ev.luna_code import looks_like_code_explain, spoken_project_catalog

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    code_home = tmp_path / "Code"
    wish_root = _seed_wish_project(code_home / "wish")
    _seed_project(code_home / "ev")
    monkeypatch.setattr(settings, "code_workspace", str(sandbox))
    monkeypatch.setattr(settings, "code_projects_root", str(code_home))
    clear_sticky_project()
    ask = "tell me about the code that i have written in the wish workspace"
    assert looks_like_code_explain(ask)
    assert not looks_like_long_code_goal(ask)
    assert maybe_handle_code_ops(ask) is None
    assert select_project(ask) == wish_root.resolve()
    assert select_project("give me info about the wish workspace") == wish_root.resolve()
    assert select_project("give me information about wish workspace") == wish_root.resolve()
    assert select_project("tell me about the ev repo") == (code_home / "ev").resolve()
    assert select_project("I wish you would tell me about the weather") == sandbox.resolve()
    catalog = spoken_project_catalog().lower()
    assert "wish" in catalog
    assert "ev" in catalog
    listed = maybe_handle_code_ops("what projects do I have")
    assert listed is not None
    assert "wish" in listed.lower()
    assert "sweet potato" in listed.lower() or "three" in listed.lower()


def test_info_and_details_do_not_start_background_work(
    tmp_path: Path, monkeypatch
) -> None:
    from app.ev.code_studio import load_studio, looks_like_long_code_goal, maybe_handle_code_ops
    from app.ev.luna_code import is_read_only_code_ask, maybe_enqueue_code_intern

    _literacy_env(tmp_path, monkeypatch)
    ask = "give me info and some details about the wish project"
    assert is_read_only_code_ask(ask)
    assert not looks_like_long_code_goal(ask)
    assert maybe_handle_code_ops(ask) is None
    assert maybe_enqueue_code_intern(ask) is None
    assert load_studio() is None or str((load_studio() or {}).get("status") or "") not in {
        "queued",
        "running",
    }


@pytest.mark.asyncio
async def test_info_about_wish_is_spoken_not_backgrounded(
    tmp_path: Path, monkeypatch
) -> None:
    from app.ev.code_studio import load_studio

    _literacy_env(tmp_path, monkeypatch)
    monkeypatch.setattr("app.gateway.muse.muse_brain_active", lambda: False)
    ask = "give me info and some details about the wish project"
    result = await run_code_job(ask)
    spoken = str(result.get("spoken") or "").lower()
    assert result.get("ok") is True
    assert result.get("project") == "wish"
    assert "sweet potato" in spoken or "wish" in spoken
    assert "background" not in spoken
    assert "writing that" not in spoken
    assert "i'll tell you when it's saved" not in spoken
    studio = load_studio()
    assert studio is None or str(studio.get("status") or "") not in {"queued", "running"}


@pytest.mark.asyncio
async def test_offline_surveys_wish_workspace_without_writing(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings
    from app.ev.code_runtime import clear_sticky_project

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    code_home = tmp_path / "Code"
    wish_root = _seed_wish_project(code_home / "wish")
    monkeypatch.setattr(settings, "code_workspace", str(sandbox))
    monkeypatch.setattr(settings, "code_projects_root", str(code_home))
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "code_model", "")
    monkeypatch.setattr("app.gateway.muse.muse_brain_active", lambda: False)
    monkeypatch.setattr("app.gateway.muse.muse_spark_key_loaded", lambda: False)
    clear_sticky_project()
    ask = "tell me about the code that i have written in the wish workspace"
    result = await run_code_job(ask)
    assert result["ok"] is True
    assert result.get("project") == "wish"
    spoken = str(result.get("spoken") or "")
    lowered = spoken.lower()
    assert "wish" in lowered
    assert "sweet potato" in lowered
    assert "next" in lowered or "three" in lowered
    assert "overview.md" not in lowered
    assert "package.json" not in lowered
    from app.ev.luna_code import _spoken_is_file_dump

    assert not _spoken_is_file_dump(spoken)
    assert "hello.py" not in lowered
    assert not result.get("files_changed")
    assert not (wish_root / "hello.py").exists()
    assert not (sandbox / "hello.py").exists()


@pytest.mark.asyncio
async def test_offline_catalogs_code_projects(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings
    from app.ev.code_runtime import clear_sticky_project, sticky_project_path

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    code_home = tmp_path / "Code"
    _seed_wish_project(code_home / "wish")
    _seed_project(code_home / "ev")
    monkeypatch.setattr(settings, "code_workspace", str(sandbox))
    monkeypatch.setattr(settings, "code_projects_root", str(code_home))
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "code_model", "")
    monkeypatch.setattr("app.gateway.muse.muse_brain_active", lambda: False)
    monkeypatch.setattr("app.gateway.muse.muse_spark_key_loaded", lambda: False)
    clear_sticky_project()
    result = await run_code_job("what projects do I have")
    assert result["ok"] is True
    spoken = str(result.get("spoken") or "").lower()
    assert "wish" in spoken
    assert "ev" in spoken
    assert not result.get("files_changed")
    assert sticky_project_path() is None
    assert "sweet potato" in spoken or "three" in spoken or "python" in spoken


@pytest.mark.asyncio
async def test_info_about_named_workspace_is_not_the_sandbox(
    tmp_path: Path, monkeypatch
) -> None:
    from app.config import settings
    from app.ev.code_runtime import clear_sticky_project

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "hello.py").write_text("print('sandbox')\n", encoding="utf-8")
    code_home = tmp_path / "Code"
    wish_root = _seed_wish_project(code_home / "wish")
    zombie = code_home / "zombie-game"
    zombie.mkdir(parents=True)
    (zombie / "README.md").write_text("# Zombie game\nArcade shooter.\n", encoding="utf-8")
    certify = _seed_project(code_home / "certify")
    monkeypatch.setattr(settings, "code_workspace", str(sandbox))
    monkeypatch.setattr(settings, "code_projects_root", str(code_home))
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "code_model", "")
    monkeypatch.setattr("app.gateway.muse.muse_brain_active", lambda: False)
    monkeypatch.setattr("app.gateway.muse.muse_spark_key_loaded", lambda: False)
    clear_sticky_project()
    remember_code_job(
        {
            "ok": True,
            "workspace": str(sandbox),
            "project": "code-workspace",
            "files_changed": ["hello.py"],
            "spoken": "Wrote hello.py.",
            "goal": "write a python script that prints hello world",
        },
        session_key="owner",
    )
    result = await run_code_job("give me info about the wish workspace")
    assert result["ok"] is True
    assert result.get("project") == "wish"
    spoken = str(result.get("spoken") or "").lower()
    assert "wish" in spoken
    assert "code-workspace" not in spoken
    assert "ev coding folder" not in spoken
    assert "sweet potato" in spoken
    assert "overview.md" not in spoken
    assert "package.json" not in spoken
    assert not (wish_root / "hello.py").exists()

    unnamed = await run_code_job("tell me about the workspace")
    unnamed_spoken = str(unnamed.get("spoken") or "").lower()
    assert "which project" in unnamed_spoken or "wish" in unnamed_spoken
    assert "code-workspace" not in unnamed_spoken

    z_result = await run_code_job("tell me about the zombie game folder")
    assert z_result.get("project") == "zombie-game"
    assert "arcade" in str(z_result.get("spoken") or "").lower() or "zombie" in str(
        z_result.get("spoken") or ""
    ).lower()

    c_result = await run_code_job("give me information about certify")
    assert c_result.get("project") == "certify"
    assert certify.name in str(c_result.get("workspace") or "")
    certify_spoken = str(c_result.get("spoken") or "").lower()
    assert "python" in certify_spoken
    assert "add" in certify_spoken
    assert "mathy.py" not in certify_spoken


def test_spoken_is_file_dump_detects_listings_not_purpose() -> None:
    from app.ev.luna_code import _spoken_is_file_dump

    assert _spoken_is_file_dump(
        "In wish: OVERVIEW.md, package.json, src/, README.md, next.config.ts"
    )
    assert _spoken_is_file_dump("I looked through wish.")
    assert not _spoken_is_file_dump(
        "Wish is Sweet Potato, a Next.js and Three.js birthday film."
    )


@pytest.mark.asyncio
async def test_named_explain_skips_spark_when_purpose_is_known(
    tmp_path: Path, monkeypatch
) -> None:
    from app.config import settings
    from app.ev.code_runtime import clear_sticky_project

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    code_home = tmp_path / "Code"
    _seed_wish_project(code_home / "wish")
    monkeypatch.setattr(settings, "code_workspace", str(sandbox))
    monkeypatch.setattr(settings, "code_projects_root", str(code_home))
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "code_model", "")
    monkeypatch.setattr("app.gateway.muse.muse_brain_active", lambda: True)
    monkeypatch.setattr("app.gateway.muse.muse_spark_key_loaded", lambda: True)
    monkeypatch.setattr("app.gateway.muse.muse_spark_model", lambda: "muse-spark-1.3-contributor")

    async def boom(*_args, **_kwargs):
        raise AssertionError("spark should not run for a purpose-ready explain")

    monkeypatch.setattr("app.ev.luna_code._spark_code_loop", boom)
    clear_sticky_project()
    result = await run_code_job("give me info about the wish workspace")
    spoken = str(result.get("spoken") or "").lower()
    assert result["ok"] is True
    assert result.get("project") == "wish"
    assert "sweet potato" in spoken
    assert "overview.md" not in spoken


@pytest.mark.asyncio
async def test_spark_file_dump_explain_is_replaced_with_purpose(
    tmp_path: Path, monkeypatch
) -> None:
    from app.config import settings
    from app.ev.code_runtime import clear_sticky_project

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    code_home = tmp_path / "Code"
    notes = code_home / "notes"
    notes.mkdir(parents=True)
    (notes / "scratch.log").write_text("log\n", encoding="utf-8")
    monkeypatch.setattr(settings, "code_workspace", str(sandbox))
    monkeypatch.setattr(settings, "code_projects_root", str(code_home))
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "code_model", "")
    monkeypatch.setattr(settings, "code_max_steps", 8)
    monkeypatch.setattr("app.gateway.muse.muse_brain_active", lambda: True)
    monkeypatch.setattr("app.gateway.muse.muse_spark_key_loaded", lambda: True)
    monkeypatch.setattr("app.gateway.muse.muse_spark_model", lambda: "muse-spark-1.3-contributor")

    async def dump_loop(*_args, **_kwargs):
        return {
            "ok": True,
            "spoken": "In notes: scratch.log, README.md, package.json, src/, app/",
            "files_changed": [],
            "runs": [],
            "workspace": str(notes.resolve()),
        }

    monkeypatch.setattr("app.ev.luna_code._spark_code_loop", dump_loop)
    clear_sticky_project()
    result = await run_code_job("give me info about the notes workspace")
    spoken = str(result.get("spoken") or "").lower()
    assert result.get("project") == "notes"
    assert "scratch.log" not in spoken
    assert "package.json" not in spoken
    from app.ev.luna_code import _spoken_is_file_dump

    assert not _spoken_is_file_dump(str(result.get("spoken") or ""))


def _literacy_env(tmp_path: Path, monkeypatch):
    from app.config import settings
    from app.ev.code_runtime import clear_sticky_project

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    code_home = tmp_path / "Code"
    wish_root = _seed_wish_project(code_home / "wish")
    certify = _seed_project(code_home / "certify")
    monkeypatch.setattr(settings, "code_workspace", str(sandbox))
    monkeypatch.setattr(settings, "code_projects_root", str(code_home))
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "code_model", "")
    monkeypatch.setattr("app.gateway.muse.muse_brain_active", lambda: False)
    monkeypatch.setattr("app.gateway.muse.muse_spark_key_loaded", lambda: False)
    from app.ev.code_sandbox import reset_folder_map
    from app.ev.luna_code import _LAST_CODE_JOBS

    reset_folder_map()
    _LAST_CODE_JOBS.clear()
    clear_sticky_project()
    return sandbox, wish_root, certify


def test_code_literacy_routing_does_not_steal_chats(tmp_path: Path, monkeypatch) -> None:
    from app.ev.code_literacy import looks_like_code_how_to_run, looks_like_code_literacy
    from app.ev.luna_code import looks_like_code_request
    from app.ev.tool_select import resolve_live_action

    _literacy_env(tmp_path, monkeypatch)
    run_wish = "how do I run the wish workspace"
    assert looks_like_code_how_to_run(run_wish)
    assert looks_like_code_literacy(run_wish)
    assert looks_like_code_request(run_wish)
    assert resolve_live_action(run_wish) == ("code", {"goal": run_wish})
    teddy = "where is the teddy in the wish workspace"
    assert looks_like_code_request(teddy)
    assert not looks_like_code_request("I wish you would tell me about the weather")
    assert not looks_like_code_request("tell me about my chats")
    assert not looks_like_code_literacy("where is John")
    assert looks_like_code_request("how is the wish workspace structured")
    assert looks_like_code_request("what is the wish folder")


@pytest.mark.asyncio
async def test_named_folder_and_file_lookup_answers_from_the_tree(
    tmp_path: Path, monkeypatch
) -> None:
    from app.ev.code_locate import resolve_code_target
    from app.ev.code_runtime import clear_sticky_project, remember_sticky_project
    from app.ev.luna_code import looks_like_code_explain, looks_like_code_request
    from app.ev.tool_select import resolve_live_action

    _, wish_root, _certify = _literacy_env(tmp_path, monkeypatch)
    _seed_project(tmp_path / "Code" / "tryon")

    assert looks_like_code_explain("what is the wish folder")
    assert looks_like_code_request("what is tryon")
    assert resolve_live_action("what is the wish folder") == (
        "code",
        {"goal": "what is the wish folder"},
    )
    assert resolve_live_action("help me understand the tryon project")[0] == "code"
    assert not looks_like_code_request("what is the weather")
    assert not looks_like_code_request("where is John")

    folder = await run_code_job("what is the wish folder")
    spoken = str(folder.get("spoken") or "").lower()
    assert folder.get("project") == "wish"
    assert "sweet potato" in spoken
    assert "overview.md" not in spoken

    about_tryon = await run_code_job("what is tryon")
    assert about_tryon.get("project") == "tryon"
    assert "add" in str(about_tryon.get("spoken") or "").lower()

    find_wish = await run_code_job("find the wish folder")
    find_spoken = str(find_wish.get("spoken") or "").lower()
    assert find_wish.get("project") == "wish"
    assert "sweet potato" in find_spoken
    assert "i don't see" not in find_spoken

    experience = await run_code_job("tell me about the experience folder")
    experience_spoken = str(experience.get("spoken") or "").lower()
    assert experience.get("project") == "wish"
    assert "src/experience" in experience_spoken
    assert "experience.tsx" not in experience_spoken

    teddy = await run_code_job("tell me about PlushTeddy")
    teddy_spoken = str(teddy.get("spoken") or "").lower()
    assert teddy.get("project") == "wish"
    assert "plushteddy" in teddy_spoken.replace(" ", "")
    assert "src/" in teddy_spoken or "experience" in teddy_spoken

    where = await run_code_job("where is PlushTeddy")
    assert where.get("project") == "wish"
    assert "plushteddy" in str(where.get("spoken") or "").lower().replace(" ", "")

    understand = await run_code_job("help me understand the tryon project")
    assert understand.get("project") == "tryon"

    target = resolve_code_target("what is the wish folder")
    assert target is not None and target.project == "wish" and not target.rel

    clear_sticky_project()
    remember_sticky_project(wish_root)
    inner = await run_code_job("what's in the experience folder")
    assert inner.get("project") == "wish"
    assert "experience" in str(inner.get("spoken") or "").lower()



@pytest.mark.asyncio
async def test_alias_catalog_run_search_git_and_sticky_followups(
    tmp_path: Path, monkeypatch
) -> None:
    import subprocess

    from app.ev.code_literacy import project_name_for_alias
    from app.ev.code_runtime import remember_sticky_project, select_project
    from app.ev.luna_code import spoken_project_catalog

    _, wish_root, _certify = _literacy_env(tmp_path, monkeypatch)
    subprocess.run(["git", "init"], cwd=wish_root, check=True, capture_output=True)
    (wish_root / "scratch.txt").write_text("dirty\n", encoding="utf-8")

    catalog = spoken_project_catalog().lower()
    assert "wish" in catalog
    assert "sweet potato" in catalog or "three" in catalog
    assert "overview.md" not in catalog
    assert project_name_for_alias("tell me about sweet potato") == "wish"
    assert select_project("give me info about the wish workspace") == wish_root.resolve()
    assert select_project("tell me about sweet potato") == wish_root.resolve()

    alias = await run_code_job("tell me about sweet potato")
    assert alias.get("project") == "wish"
    spoken = str(alias.get("spoken") or "").lower()
    assert "sweet potato" in spoken
    assert "overview.md" not in spoken

    run = await run_code_job("how do I run the wish workspace")
    run_spoken = str(run.get("spoken") or "").lower()
    assert run.get("project") == "wish"
    assert "npm run dev" in run_spoken
    assert "cannot run npm" in run_spoken
    assert "overview.md" not in run_spoken

    teddy = await run_code_job("where is the teddy in the wish workspace")
    teddy_spoken = str(teddy.get("spoken") or "").lower()
    assert "teddy" in teddy_spoken
    assert "plushteddy" in teddy_spoken.replace(" ", "") or "experience" in teddy_spoken

    structure = await run_code_job("how is the wish workspace structured")
    structure_spoken = str(structure.get("spoken") or "").lower()
    assert structure.get("project") == "wish"
    assert "experience" in structure_spoken or "src/app" in structure_spoken
    assert "overview.md" not in structure_spoken

    dirty = await run_code_job("what's dirty in the wish workspace")
    dirty_spoken = str(dirty.get("spoken") or "").lower()
    assert "scratch.txt" in dirty_spoken or "uncommitted" in dirty_spoken

    remember_sticky_project(wish_root)
    follow = await run_code_job("how do I run it")
    assert follow.get("project") == "wish"
    assert "npm run dev" in str(follow.get("spoken") or "").lower()

    from app.ev.code_literacy import looks_like_code_literacy

    assert looks_like_code_literacy("where is the teddy")
    assert not looks_like_code_literacy("where is John")
    sticky_teddy = await run_code_job("where is the teddy")
    sticky_teddy_spoken = str(sticky_teddy.get("spoken") or "").lower()
    assert sticky_teddy.get("project") == "wish"
    assert "plushteddy" in sticky_teddy_spoken.replace(" ", "") or "experience" in sticky_teddy_spoken

    deeper = await run_code_job("go deeper")
    deeper_spoken = str(deeper.get("spoken") or "").lower()
    assert deeper.get("project") == "wish"
    assert "sweet potato" in deeper_spoken
    assert "src/" in deeper_spoken or "experience" in deeper_spoken or "npm run" in deeper_spoken


def test_purpose_catalog_ranks_named_work_ahead_of_clones(tmp_path: Path, monkeypatch) -> None:
    from app.ev.luna_code import spoken_project_catalog

    _sandbox, _wish_root, _certify = _literacy_env(tmp_path, monkeypatch)
    code_home = tmp_path / "Code"
    for name in ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"):
        _seed_project(code_home / name)
    clone = code_home / "ev-remote.git"
    clone.mkdir()
    (clone / "README.md").write_text("clone of ev\n", encoding="utf-8")

    catalog = spoken_project_catalog().lower()
    assert "wish" in catalog
    assert "sweet potato" in catalog
    assert "overview.md" not in catalog
    assert "ev-remote.git" not in catalog
    wish_at = catalog.find("wish")
    hotel_at = catalog.find("hotel")
    assert wish_at >= 0
    assert hotel_at == -1 or wish_at < hotel_at


def test_spark_code_loop_injects_purpose_card() -> None:
    import inspect

    from app.ev.luna_code import _spark_code_loop

    source = inspect.getsource(_spark_code_loop)
    assert "This repo's purpose" in source
    assert "project_card" in source
    assert "_folder_map_block" in source
    from app.ev.luna_code import LUNA_CODE_TOOLS, _folder_map_block

    assert any(item.get("name") == "lookup_folder" for item in LUNA_CODE_TOOLS)
    assert "Folder map" in inspect.getsource(_folder_map_block)


def test_git_relpath_strips_status_not_folder_name() -> None:
    from pathlib import Path

    from app.ev.code_literacy import _git_relpath

    assert _git_relpath(" M scripts/capture.cjs") == "scripts/capture.cjs"
    assert Path(_git_relpath(" M scripts/capture.cjs")).parts[0] == "scripts"
    assert _git_relpath("?? OVERVIEW.md") == "OVERVIEW.md"
    assert _git_relpath("R  old.ts -> src/new.ts") == "src/new.ts"


@pytest.mark.asyncio
async def test_folder_sandbox_hits_misses_and_skips_spark(
    tmp_path: Path, monkeypatch
) -> None:
    from app.ev.code_locate import looks_like_named_place_ask, resolve_code_target
    from app.ev.code_runtime import select_project
    from app.ev.code_sandbox import lookup_folder_name, project_map_brief, reset_folder_map
    from app.ev.luna_code import looks_like_code_request
    from app.ev.tool_select import resolve_live_action

    sandbox, wish_root, _certify = _literacy_env(tmp_path, monkeypatch)
    tryon = _seed_project(tmp_path / "Code" / "tryon")
    reset_folder_map()

    assert looks_like_named_place_ask("what is the foobarbaz folder")
    assert not looks_like_code_request("what is the foobarbaz folder")
    assert resolve_live_action("what is the foobarbaz folder")[0] == "computer"
    assert resolve_live_action("add a test in the foobarbaz folder")[0] == "code"
    assert not looks_like_code_request("what is the weather")
    assert not looks_like_code_request("tell me about my chats")
    assert not looks_like_named_place_ask("where is John")

    hits = lookup_folder_name("experience")
    assert hits
    assert any(item.get("rel") == "src/experience" for item in hits)
    brief = project_map_brief(wish_root)
    assert "src/experience" in brief

    experience = resolve_code_target("what's in the experience folder")
    assert experience is not None and experience.project == "wish"
    assert experience.rel == "src/experience"

    assert select_project("add a test in the tryon folder") == tryon.resolve()
    assert select_project("find the Invoices folder") == sandbox.resolve()

    spark_calls: list[str] = []

    async def boom(goal: str, **_kwargs):
        spark_calls.append(goal)
        raise AssertionError("spark must not hunt an unknown folder")

    monkeypatch.setattr("app.ev.luna_code._spark_code_loop", boom)
    monkeypatch.setattr("app.gateway.muse.muse_brain_active", lambda: True)
    monkeypatch.setattr("app.gateway.muse.muse_spark_key_loaded", lambda: True)

    miss = await run_code_job("what is the foobarbaz folder")
    miss_spoken = str(miss.get("spoken") or "").lower()
    assert miss.get("error") == "unknown_folder"
    assert miss.get("brain") in {"locate", "hub"}
    assert "foobarbaz" in miss_spoken
    assert "don't see" in miss_spoken
    assert spark_calls == []

    write_miss = await run_code_job("add a test in the foobarbaz folder")
    assert write_miss.get("error") == "unknown_folder"
    assert "don't see" in str(write_miss.get("spoken") or "").lower()
    assert write_miss.get("files_changed") == []
    assert spark_calls == []

    switched = await run_code_job("what is the tryon folder")
    assert switched.get("project") == "tryon"
    assert spark_calls == []

    found = await run_code_job("find the experience folder")
    found_spoken = str(found.get("spoken") or "").lower()
    assert found.get("project") == "wish"
    assert "src/experience" in found_spoken
    assert spark_calls == []


@pytest.mark.asyncio
async def test_desk_folder_is_spoken_not_jailed(tmp_path: Path, monkeypatch) -> None:
    from app.config import settings
    from app.ev.code_locate import resolve_code_target
    from app.ev.code_runtime import select_project
    from app.ev.code_sandbox import reset_folder_map

    sandbox, _wish, _certify = _literacy_env(tmp_path, monkeypatch)
    desk = tmp_path / "Desktop"
    invoices = desk / "Invoices"
    invoices.mkdir(parents=True)
    (invoices / "april.txt").write_text("receipt\n", encoding="utf-8")
    monkeypatch.setattr(settings, "environment", "dev")
    monkeypatch.setattr("app.ev.code_sandbox._desk_roots", lambda: [desk])
    reset_folder_map()

    target = resolve_code_target("find the Invoices folder")
    assert target is not None and target.kind == "desk"
    assert select_project("find the Invoices folder") == sandbox.resolve()

    spark_calls: list[str] = []

    async def boom(goal: str, **_kwargs):
        spark_calls.append(goal)
        raise AssertionError("spark must not jail a desk folder")

    monkeypatch.setattr("app.ev.luna_code._spark_code_loop", boom)
    monkeypatch.setattr("app.gateway.muse.muse_brain_active", lambda: True)
    monkeypatch.setattr("app.gateway.muse.muse_spark_key_loaded", lambda: True)

    result = await run_code_job("find the Invoices folder")
    spoken = str(result.get("spoken") or "").lower()
    assert result.get("brain") in {"locate", "hub"}
    assert "invoices" in spoken
    assert "desktop" in spoken
    assert spark_calls == []
    assert result.get("files_changed") == []


@pytest.mark.asyncio
async def test_explain_finds_project_outside_the_code_folder(
    tmp_path: Path, monkeypatch
) -> None:
    from app.ev.code_locate import resolve_code_target
    from app.ev.code_runtime import select_project
    from app.ev.code_sandbox import reset_folder_map
    from app.ev.luna_code import looks_like_code_explain, looks_like_code_request
    from app.ev.tool_select import resolve_live_action

    sandbox, wish_root, _certify = _literacy_env(tmp_path, monkeypatch)
    northstar = tmp_path / "Desktop" / "northstar"
    northstar.mkdir(parents=True)
    (northstar / ".git").mkdir()
    (northstar / "package.json").write_text(
        '{"name":"northstar","description":"Fleet navigation desk app"}\n',
        encoding="utf-8",
    )
    (northstar / "README.md").write_text(
        "# Northstar\n\nFleet navigation for the owner laptop.\n",
        encoding="utf-8",
    )
    invoices = tmp_path / "Desktop" / "Invoices"
    invoices.mkdir()
    (invoices / "april.txt").write_text("receipt\n", encoding="utf-8")
    lumen = tmp_path / "Documents" / "lumen"
    lumen.mkdir(parents=True)
    (lumen / ".git").mkdir()
    (lumen / "README.md").write_text("# Lumen\nNotes app for this Mac.\n", encoding="utf-8")
    reset_folder_map()

    ask = "give me info about the northstar project"
    laptop_ask = "give me info about the northstar project on my laptop"
    assert looks_like_code_request(ask)
    assert looks_like_code_explain(ask)
    assert looks_like_code_request(laptop_ask)
    assert looks_like_code_explain(laptop_ask)
    assert resolve_live_action(ask) == ("code", {"goal": ask})
    assert resolve_live_action(laptop_ask)[0] == "code"

    target = resolve_code_target(ask)
    assert target is not None and target.kind == "project"
    assert target.root == northstar.resolve()
    assert select_project(ask) == northstar.resolve()
    assert select_project(laptop_ask) == northstar.resolve()
    assert select_project("tell me about the wish workspace") == wish_root.resolve()
    assert select_project("give me info about the lumen project") == lumen.resolve()
    assert select_project("find the Invoices folder") == sandbox.resolve()
    assert resolve_code_target("find the Invoices folder") is None or (
        resolve_code_target("find the Invoices folder").kind == "desk"
    )

    spark_calls: list[str] = []

    async def boom(goal: str, **_kwargs):
        spark_calls.append(goal)
        raise AssertionError("spark must not hunt a named laptop project")

    monkeypatch.setattr("app.ev.luna_code._spark_code_loop", boom)
    monkeypatch.setattr("app.gateway.muse.muse_brain_active", lambda: True)
    monkeypatch.setattr("app.gateway.muse.muse_spark_key_loaded", lambda: True)

    result = await run_code_job(ask)
    spoken = str(result.get("spoken") or "").lower()
    assert result.get("ok") is True
    assert result.get("project") == "northstar"
    assert "northstar" in spoken
    assert "fleet" in spoken or "navigation" in spoken
    assert "code folder" not in spoken
    assert spark_calls == []

    laptop_result = await run_code_job(laptop_ask)
    assert laptop_result.get("project") == "northstar"
    assert "code folder" not in str(laptop_result.get("spoken") or "").lower()

    lumen_result = await run_code_job("give me info about the lumen project")
    assert lumen_result.get("project") == "lumen"
    assert "notes" in str(lumen_result.get("spoken") or "").lower() or "lumen" in str(
        lumen_result.get("spoken") or ""
    ).lower()

    miss = await run_code_job("give me info about the zephyrnine project")
    miss_spoken = str(miss.get("spoken") or "").lower()
    assert "don't see" in miss_spoken or "dont see" in miss_spoken
    assert "zephyrnine" in miss_spoken
    assert "code folder" not in miss_spoken
    assert spark_calls == []


def test_desktop_cue_picks_the_laptop_copy_not_just_code(
    tmp_path: Path, monkeypatch
) -> None:
    from app.ev.code_runtime import select_project
    from app.ev.code_sandbox import reset_folder_map

    _literacy_env(tmp_path, monkeypatch)
    code_one = _seed_project(tmp_path / "Code" / "northstar")
    (code_one / "README.md").write_text("# Code copy\nLives in Code.\n", encoding="utf-8")
    desk = tmp_path / "Desktop" / "northstar"
    desk.mkdir(parents=True)
    (desk / ".git").mkdir()
    (desk / "README.md").write_text("# Desk copy\nLives on Desktop.\n", encoding="utf-8")
    reset_folder_map()
    assert (
        select_project("give me info about the northstar project on my desktop")
        == desk.resolve()
    )
    assert select_project("give me info about the northstar project") == code_one.resolve()


def test_last_code_folder_does_not_steal_general_questions(
    tmp_path: Path, monkeypatch
) -> None:
    from app.ev.code_locate import resolve_code_target
    from app.ev.code_runtime import remember_sticky_project
    from app.ev.luna_code import looks_like_code_explain, looks_like_code_request
    from app.ev.tool_select import resolve_live_action

    _, _wish, _certify = _literacy_env(tmp_path, monkeypatch)
    tryon = _seed_project(tmp_path / "Code" / "tryon")
    remember_sticky_project(tryon)

    assert looks_like_code_request("what is the tryon folder")
    assert looks_like_code_request("how do I run it")
    assert looks_like_code_explain("help me understand the tryon project")

    for ask in (
        "what is clothing",
        "tell me about garments",
        "explain gravity",
        "tell me more",
        "what is a virtual try-on platform",
        "how are you",
        "what should I have for dinner",
    ):
        assert not looks_like_code_request(ask), ask
        assert not looks_like_code_explain(ask), ask
        assert resolve_live_action(ask) != ("code", {"goal": ask})
        live = resolve_live_action(ask)
        assert live is None or live[0] not in {"code", "computer"}, (ask, live)

    assert resolve_code_target("what is clothing") is None
    assert resolve_code_target("tell me about garments") is None
    assert resolve_code_target("what is the tryon folder") is not None


@pytest.mark.asyncio
async def test_code_job_refuses_general_questions_even_if_forced(
    tmp_path: Path, monkeypatch
) -> None:
    from app.ev.code_runtime import remember_sticky_project
    from app.ev.luna_code import is_code_lane_ask, run_code_job
    from app.ev.tools import get_spec

    _, _wish, _certify = _literacy_env(tmp_path, monkeypatch)
    tryon = _seed_project(tmp_path / "Code" / "tryon")
    remember_sticky_project(tryon)
    spec = get_spec("code")
    assert spec is not None
    assert "general knowledge" in str(spec.get("description") or "").lower()
    assert "casual phrasing" not in str(spec.get("description") or "").lower()

    for ask in ("explain gravity", "what is clothing", "tell me about garments"):
        assert not is_code_lane_ask(ask), ask
        result = await run_code_job(ask)
        assert result.get("error") == "not_a_code_job", ask
        assert result.get("files_changed") == []
        assert result.get("brain") == "chat"

    named = await run_code_job("what is the tryon folder")
    assert named.get("project") == "tryon"
    assert named.get("error") != "not_a_code_job"


@pytest.mark.asyncio
async def test_named_project_wins_over_sticky_and_unknown_misses_without_spark(
    tmp_path: Path, monkeypatch
) -> None:
    from types import SimpleNamespace

    from app.ev.code_locate import (
        looks_like_named_place_ask,
        preferred_catalog_projects,
        wanted_place_names,
    )
    from app.ev.code_runtime import remember_sticky_project, select_project
    from app.ev.code_sandbox import lookup_folder_name, reset_folder_map
    from app.ev.luna_code import looks_like_code_request
    from app.ev.tool_select import resolve_live_action
    from app.ev.turn import snapshot_working_on

    sandbox, wish_root, _certify = _literacy_env(tmp_path, monkeypatch)
    tryon = _seed_project(tmp_path / "Code" / "tryon")
    (tryon / "OVERVIEW.md").write_text(
        "## What it is\ntryon is a virtual fitting room.\n",
        encoding="utf-8",
    )
    reset_folder_map()
    remember_sticky_project(tryon)

    other = "tell me about the wish project"
    switch = "I do not want tryon, I want you to tell me about the wish project"
    unknown = "tell me about the foobarbaz project"
    bare_unknown = "tell me about foobarbaz project"

    assert looks_like_named_place_ask(unknown)
    assert looks_like_named_place_ask(bare_unknown)
    assert looks_like_code_request(other)
    assert looks_like_code_request(switch)
    assert looks_like_code_request(unknown)
    assert resolve_live_action(unknown)[0] == "code"
    assert not looks_like_named_place_ask("history project due tomorrow")
    assert resolve_live_action("explain gravity") is None or resolve_live_action(
        "explain gravity"
    )[0] not in {"code", "computer"}

    assert wanted_place_names(switch)[0].lower() == "wish"
    assert "tryon" not in {item.lower() for item in wanted_place_names(switch)}
    assert preferred_catalog_projects(switch) == ["wish"]
    assert preferred_catalog_projects(other) == ["wish"]
    assert preferred_catalog_projects(unknown) == []
    assert select_project(other) == wish_root.resolve()
    assert select_project(switch) == wish_root.resolve()
    assert select_project(unknown) == sandbox.resolve()

    briefing = snapshot_working_on(
        other,
        user_state=SimpleNamespace(
            current_task=None,
            active_project="tryon",
            active_goal=None,
            activity=None,
            recent_topics=[],
        ),
        continuation=False,
    )
    assert "Active project: tryon" not in briefing

    ranked = lookup_folder_name("plushtedd")
    assert ranked
    assert any("plushteddy" in str(item.get("rel") or "").lower() for item in ranked)

    spark_calls: list[str] = []

    async def boom(goal: str, **_kwargs):
        spark_calls.append(goal)
        raise AssertionError("spark must not hunt a named info ask")

    monkeypatch.setattr("app.ev.luna_code._spark_code_loop", boom)
    monkeypatch.setattr("app.gateway.muse.muse_brain_active", lambda: True)
    monkeypatch.setattr("app.gateway.muse.muse_spark_key_loaded", lambda: True)

    wish = await run_code_job(other)
    wish_spoken = str(wish.get("spoken") or "").lower()
    assert wish.get("project") == "wish"
    assert "sweet potato" in wish_spoken or "birthday" in wish_spoken
    assert "fitting room" not in wish_spoken
    assert spark_calls == []

    switched = await run_code_job(switch)
    switched_spoken = str(switched.get("spoken") or "").lower()
    assert switched.get("project") == "wish"
    assert "fitting room" not in switched_spoken
    assert spark_calls == []

    miss = await run_code_job(unknown)
    miss_spoken = str(miss.get("spoken") or "").lower()
    assert miss.get("error") == "unknown_folder"
    assert "foobarbaz" in miss_spoken
    assert "don't see" in miss_spoken
    assert "fitting room" not in miss_spoken
    assert spark_calls == []

    bare_miss = await run_code_job(bare_unknown)
    assert bare_miss.get("error") == "unknown_folder"
    assert "foobarbaz" in str(bare_miss.get("spoken") or "").lower()
    assert spark_calls == []

    general = await run_code_job("explain gravity")
    assert general.get("error") == "not_a_code_job"
    assert spark_calls == []





