"""Evie coding broker: Luna brain, bounded workspace, live/chat dispatch."""

from __future__ import annotations

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

    async def complete_raw(self, messages, *, tools=None, model=None, response_format=None, tool_choice=None):
        self.calls.append([dict(message) for message in messages])
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

