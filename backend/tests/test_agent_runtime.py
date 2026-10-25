"""Hermetic tests for the general agent runtime (no network, no real brain).

A scripted provider stands in for the model so the loop's contract is tested
exactly: tool calls execute through the registry, mutating calls park for one
owner decision, hard DENY verdicts are final, budgets and cancellation stop
the run, and a claim the receipts do not support fails the run instead of
being spoken as success.
"""

from __future__ import annotations

import hashlib
from collections import deque
from pathlib import Path

import pytest

from app.agent import tools as agent_tools
from app.agent.brain import resolve_agent_brain
from app.agent.loop import (
    AgentLimits,
    ApproveGate,
    CancelToken,
    DenyGate,
    QueueGate,
    run_agent,
)
from app.agent.tools import AgentTool, ToolContext
from app.config import settings
from app.contracts import ChatResult, ToolCall


class ScriptedProvider:
    """Deterministic stand-in for a chat provider with tool calls."""

    name = "scripted"
    supports_media = False

    def __init__(self, turns: list[ChatResult]) -> None:
        self.turns = deque(turns)
        self.seen: list[list] = []

    async def chat_with_tools(self, messages, tools, *, model=None, temperature=0.7):
        del tools, model, temperature
        self.seen.append(list(messages))
        if self.turns:
            return self.turns.popleft()
        return ChatResult(text="nothing left to do", model="scripted")

    async def chat(self, messages, *, model=None, temperature=0.7):
        return await self.chat_with_tools(messages, [])


def _turn(*calls: ToolCall, text: str = "") -> ChatResult:
    return ChatResult(text=text, tool_calls=list(calls), usage={}, model="scripted")


def _finish(summary: str = "done", ok: bool | None = True) -> ToolCall:
    args: dict = {"summary": summary}
    if ok is not None:
        args["ok"] = ok
    return ToolCall(id="finish-1", name="finish", arguments=args)


# --------------------------------------------------------------------------- #
# Brain resolution
# --------------------------------------------------------------------------- #


def test_no_brain_resolves_to_degraded_echo(monkeypatch) -> None:
    for key in ("xai_api_key", "deepseek_api_key", "agent_openai_api_key",
                "openrouter_api_key", "local_model_base_url"):
        monkeypatch.setattr(settings, key, None, raising=False)
    monkeypatch.setattr(settings, "agent_provider", "auto")
    choice = resolve_agent_brain()
    assert choice.provider == "echo"
    assert choice.degraded is True


def test_auto_prefers_configured_provider(monkeypatch) -> None:
    monkeypatch.setattr(settings, "agent_openai_api_key", None, raising=False)
    monkeypatch.setattr(settings, "deepseek_api_key", "ds-key", raising=False)
    monkeypatch.setattr(settings, "xai_api_key", "xai-key", raising=False)
    monkeypatch.setattr(settings, "openrouter_api_key", None, raising=False)
    monkeypatch.setattr(settings, "agent_provider", "auto")
    assert resolve_agent_brain().provider == "xai"

    monkeypatch.setattr(settings, "xai_api_key", None, raising=False)
    assert resolve_agent_brain().provider == "deepseek-direct"

    named = resolve_agent_brain("deepseek-direct")
    assert named.provider == "deepseek-direct" and named.available


def test_deepseek_is_the_default_main_brain(monkeypatch) -> None:
    monkeypatch.setattr(settings, "openrouter_api_key", "or-key", raising=False)
    monkeypatch.setattr(settings, "brain_model", "meta/muse-spark-1.3-contributor", raising=False)
    monkeypatch.setattr(settings, "agent_provider", "auto")
    choice = resolve_agent_brain()
    assert choice.provider == "muse"
    assert choice.model == "meta/muse-spark-1.3-contributor"
    assert resolve_agent_brain("openrouter").available is True


def test_named_provider_without_key_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(settings, "openrouter_api_key", None, raising=False)
    choice = resolve_agent_brain("deepseek")
    assert choice.available is False
    monkeypatch.setattr(settings, "deepseek_api_key", None, raising=False)
    direct = resolve_agent_brain("deepseek-direct")
    assert direct.available is False


def test_unknown_brain_raises(monkeypatch) -> None:
    with pytest.raises(ValueError):
        resolve_agent_brain("gpt-from-wish")


# --------------------------------------------------------------------------- #
# Loop behavior
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_loop_executes_read_tool_then_finishes(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("needle", encoding="utf-8")
    provider = ScriptedProvider([
        _turn(ToolCall(id="c1", name="read_file", arguments={"path": "note.txt"})),
        _turn(_finish("Read the file; it contains needle.")),
    ])
    result = await run_agent(
        "read note.txt", root=tmp_path, provider=provider,
        limits=AgentLimits(max_steps=5, max_tool_calls=5, max_seconds=60),
    )
    assert result.status == "completed"
    assert result.ok is True
    assert result.steps[0].tool == "read_file"
    assert result.steps[0].ok and result.steps[0].policy == "AUTO"
    assert "needle" in str(result.steps[0].result)
    # The tool result must have been fed back as a tool message.
    assert any(msg.role == "tool" for msg in provider.seen[-1])


@pytest.mark.asyncio
async def test_hard_deny_is_final_even_when_approved(tmp_path: Path) -> None:
    provider = ScriptedProvider([
        _turn(ToolCall(id="c1", name="run_shell", arguments={"cmd": "sudo ls /"})),
        _turn(_finish("I cleaned the disk")),
    ])
    result = await run_agent(
        "clean the disk", root=tmp_path, approved=True, provider=provider,
        limits=AgentLimits(max_steps=5, max_tool_calls=5, max_seconds=60),
    )
    assert result.steps[0].policy == "DENY"
    assert result.steps[0].ok is False
    assert "denied" in (result.steps[0].error or "")
    # A success claim on top of a denied step must not be spoken as success.
    assert result.ok is False
    assert result.error == "claimed success but steps failed"


@pytest.mark.asyncio
async def test_mutating_tool_denied_by_owner_is_adaptable(tmp_path: Path) -> None:
    provider = ScriptedProvider([
        _turn(ToolCall(id="c1", name="write_file",
                       arguments={"path": "x.txt", "content": "hi"})),
        _turn(_finish("The owner declined the write.", ok=False)),
    ])
    result = await run_agent(
        "write x.txt", root=tmp_path, provider=provider, gate=DenyGate(),
        approved=False,
        limits=AgentLimits(max_steps=5, max_tool_calls=5, max_seconds=60),
    )
    assert not (tmp_path / "x.txt").exists()
    assert result.steps[0].approval == "deny"
    assert result.status == "failed"
    assert result.error == "brain reported failure"


@pytest.mark.asyncio
async def test_approve_run_executes_the_write(tmp_path: Path) -> None:
    provider = ScriptedProvider([
        _turn(ToolCall(id="c1", name="write_file",
                       arguments={"path": "x.txt", "content": "hi"})),
        _turn(_finish("Wrote x.txt and it verified.")),
    ])
    result = await run_agent(
        "write x.txt", root=tmp_path, provider=provider, gate=ApproveGate(),
        approved=False,
        limits=AgentLimits(max_steps=5, max_tool_calls=5, max_seconds=60),
    )
    assert result.ok is True
    assert (tmp_path / "x.txt").read_text(encoding="utf-8") == "hi"
    assert result.steps[0].approval == "approve_run"
    assert result.steps[0].verified is True


@pytest.mark.asyncio
async def test_invalid_arguments_are_reported_not_executed(tmp_path: Path) -> None:
    provider = ScriptedProvider([
        _turn(ToolCall(id="c1", name="write_file", arguments={"path": "x.txt"})),
        _turn(_finish("could not write", ok=False)),
    ])
    result = await run_agent(
        "write x.txt", root=tmp_path, approved=True, provider=provider,
        limits=AgentLimits(max_steps=5, max_tool_calls=5, max_seconds=60),
    )
    assert not (tmp_path / "x.txt").exists()
    assert "invalid arguments" in (result.steps[0].error or "")


@pytest.mark.asyncio
async def test_prose_final_with_a_failed_step_is_not_success(tmp_path: Path) -> None:
    provider = ScriptedProvider([
        _turn(ToolCall(id="c1", name="read_file", arguments={"path": "missing.txt"})),
        _turn(text="I could not read that file."),
    ])
    result = await run_agent(
        "read missing.txt", root=tmp_path, provider=provider,
        limits=AgentLimits(max_steps=4, max_tool_calls=4, max_seconds=60),
    )
    assert result.status == "completed"
    assert result.ok is False
    assert result.error == "steps_failed"


@pytest.mark.asyncio
async def test_step_budget_caps_honestly(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("x", encoding="utf-8")
    provider = ScriptedProvider([
        _turn(ToolCall(id=f"c{i}", name="list_dir", arguments={})) for i in range(6)
    ])
    result = await run_agent(
        "loop forever", root=tmp_path, provider=provider,
        limits=AgentLimits(max_steps=2, max_tool_calls=10, max_seconds=60),
    )
    assert result.status == "capped"
    assert result.error == "step_budget"
    assert result.ok is False


@pytest.mark.asyncio
async def test_cancel_token_stops_before_any_call(tmp_path: Path) -> None:
    token = CancelToken()
    token.cancel("owner pressed stop")
    provider = ScriptedProvider([_turn(_finish("never reached"))])
    result = await run_agent(
        "do something", root=tmp_path, provider=provider, cancel=token,
        limits=AgentLimits(max_steps=3, max_tool_calls=3, max_seconds=60),
    )
    assert result.status == "cancelled"
    assert result.ok is False


@pytest.mark.asyncio
async def test_ask_owner_stops_with_a_question(tmp_path: Path) -> None:
    provider = ScriptedProvider([
        _turn(ToolCall(id="c1", name="ask_owner",
                       arguments={"question": "Which folder did you mean?"})),
    ])
    result = await run_agent(
        "delete the folder", root=tmp_path, provider=provider,
        limits=AgentLimits(max_steps=3, max_tool_calls=3, max_seconds=60),
    )
    assert result.status == "needs_input"
    assert result.needs_input is True
    assert result.question.startswith("Which folder")


@pytest.mark.asyncio
async def test_no_brain_refuses_instead_of_pretending(monkeypatch, tmp_path: Path) -> None:
    for key in ("xai_api_key", "deepseek_api_key", "agent_openai_api_key",
                "openrouter_api_key", "local_model_base_url"):
        monkeypatch.setattr(settings, key, None, raising=False)
    monkeypatch.setattr(settings, "agent_provider", "auto")
    result = await run_agent("anything", root=tmp_path)
    assert result.status == "failed"
    assert result.error == "brain_unavailable"
    assert result.ok is False


# --------------------------------------------------------------------------- #
# Tools (direct)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_write_then_read_verifies_sha256(tmp_path: Path) -> None:
    body = "line one\nline two\n"
    tool: AgentTool = agent_tools.REGISTRY["write_file"]
    ctx = ToolContext(root=tmp_path, approved=True)
    written = await tool.run({"path": "docs/a.txt", "content": body}, ctx)
    assert written["ok"] and written["verified"]
    expected = hashlib.sha256(body.encode()).hexdigest()
    assert written["result"]["sha256"] == expected

    read = await agent_tools.REGISTRY["read_file"].run({"path": "docs/a.txt"}, ctx)
    assert read["ok"] and read["result"]["content"] == body


@pytest.mark.asyncio
async def test_write_refuses_at_the_lane_without_approval(tmp_path: Path) -> None:
    result = await agent_tools.REGISTRY["write_file"].run(
        {"path": "x.txt", "content": "hi"}, ToolContext(root=tmp_path, approved=False)
    )
    assert result["ok"] is False
    assert result["needs_confirm"] is True
    assert not (tmp_path / "x.txt").exists()


@pytest.mark.asyncio
async def test_secret_path_is_denied(tmp_path: Path) -> None:
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "id_rsa").write_text("private", encoding="utf-8")
    ctx = ToolContext(root=tmp_path, approved=True)
    read = await agent_tools.REGISTRY["read_file"].run({"path": ".ssh/id_rsa"}, ctx)
    assert read["ok"] is False
    assert "secret" in (read["error"] or "")
    listed = await agent_tools.REGISTRY["list_dir"].run({"path": ".ssh"}, ctx)
    assert listed["ok"] is False


@pytest.mark.asyncio
async def test_search_files_finds_content(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\nneedle = 2\n", encoding="utf-8")
    (tmp_path / "src" / "b.md").write_text("nothing here\n", encoding="utf-8")
    result = await agent_tools.REGISTRY["search_files"].run(
        {"query": "needle", "path": "src"}, ToolContext(root=tmp_path, approved=True)
    )
    assert result["ok"] is True
    assert result["result"]["count"] >= 1
    assert any("a.py" in match["path"] for match in result["result"]["matches"])


def test_fetch_guard_blocks_loopback_and_private_hosts() -> None:
    assert agent_tools._host_blocked("localhost") is True
    assert agent_tools._host_blocked("127.0.0.1") is True
    assert agent_tools._host_blocked("10.0.0.5") is True
    assert agent_tools._host_blocked("192.168.1.10") is True


def test_tool_specs_include_controls_and_core_tools() -> None:
    names = {spec.name for spec in agent_tools.agent_tool_specs()}
    assert {"run_shell", "read_file", "write_file", "search_files", "fetch_url",
            "search_web", "open_app", "open_url", "delete_path", "list_dir",
            "observe_app", "act_in_app", "verify_app",
            "finish", "ask_owner"} <= names
    for spec in agent_tools.agent_tool_specs():
        assert spec.parameters.get("type") == "object"
        assert spec.description


# --------------------------------------------------------------------------- #
# Queue gate + HTTP surface
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_queue_gate_resolves_from_another_coroutine() -> None:
    gate = QueueGate()
    from app.agent.loop import ApprovalRequest

    async def answer():
        await __import__("asyncio").sleep(0.05)
        assert gate.waiting is True
        assert gate.decide("approve") is True

    import asyncio

    await asyncio.gather(
        gate.request(ApprovalRequest(
            run_id="r1", tool="write_file", arguments={"path": "x"},
            reason="mutating", policy="CONFIRM",
        )),
        answer(),
    )


@pytest.mark.asyncio
async def test_api_agent_brain_endpoint(client) -> None:
    resp = await client.get("/v1/agent/brain")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "auto_pick" in body and "candidates" in body


@pytest.mark.asyncio
async def test_api_run_offline_reports_honest_failure(client, monkeypatch) -> None:
    for key in ("xai_api_key", "deepseek_api_key", "agent_openai_api_key",
                "openrouter_api_key", "local_model_base_url"):
        monkeypatch.setattr(settings, key, None, raising=False)
    monkeypatch.setattr(settings, "agent_provider", "auto")
    resp = await client.post(
        "/v1/agent/runs",
        json={"goal": "do something", "wait": True, "wait_timeout_s": 10},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "done"
    assert body["result"]["error"] == "brain_unavailable"
    assert body["result"]["ok"] is False


@pytest.mark.asyncio
async def test_kernel_bridge_reports_honestly_without_a_brain(monkeypatch) -> None:
    from app.agent.kernel_bridge import start_goal_run

    for key in ("xai_api_key", "deepseek_api_key", "agent_openai_api_key",
                "openrouter_api_key", "local_model_base_url"):
        monkeypatch.setattr(settings, key, None, raising=False)
    monkeypatch.setattr(settings, "agent_provider", "auto")
    receipt = await start_goal_run("tidy my desktop", wait_seconds=5)
    assert receipt["ok"] is False
    assert "brain" in receipt["spoken"].lower() or "configured" in receipt["spoken"].lower()


def test_agent_run_is_a_registered_mutating_capability() -> None:
    from app.cognitive.capabilities import SEMANTIC_TOOLS, tool_specs
    from app.cognitive.executor import MUTATING

    names = {item["name"] for item in SEMANTIC_TOOLS}
    assert "agent.run" in names
    assert "agent.run" in MUTATING
    spec = next(item for item in tool_specs() if item.name == "agent.run")
    assert spec.risk_class == "R2"
    assert spec.read_only is False



@pytest.mark.asyncio
async def test_observe_app_compacts_the_ax_tree(monkeypatch, tmp_path: Path) -> None:
    import app.fabric.operator as operator

    monkeypatch.setattr(
        operator, "observe",
        lambda app, limit=400: {
            "ok": True, "verified": True,
            "evidence": {
                "frame_id": "fr-1", "n": 2,
                "elements": [
                    {"i": 0, "role": "AXButton", "name": "OK", "value": ""},
                    {"i": 1, "role": "AXTextField", "name": "Search", "value": "hi"},
                ],
            },
            "error": None, "undo": None,
        },
    )
    result = await agent_tools.REGISTRY["observe_app"].run(
        {"app": "Finder"}, ToolContext(root=tmp_path, approved=True)
    )
    assert result["ok"] is True
    assert result["result"]["frame_id"] == "fr-1"
    assert result["result"]["elements"][1]["role"] == "AXTextField"


@pytest.mark.asyncio
async def test_act_in_app_requires_approval_and_then_runs(monkeypatch, tmp_path: Path) -> None:
    import app.fabric.operator as operator

    calls: list[tuple] = []
    monkeypatch.setattr(
        operator, "act",
        lambda app, frame_id, action, approved=False: calls.append(
            (app, frame_id, action, approved)
        ) or {"ok": True, "verified": True, "evidence": {"kind": "click"},
              "error": None, "undo": None},
    )
    args = {"app": "Finder", "frame_id": "fr-1", "action": {"kind": "click", "ref": 0}}
    refused = await agent_tools.REGISTRY["act_in_app"].run(
        args, ToolContext(root=tmp_path, approved=False)
    )
    assert refused["ok"] is False and refused["needs_confirm"] is True
    assert calls == []

    done = await agent_tools.REGISTRY["act_in_app"].run(
        args, ToolContext(root=tmp_path, approved=True)
    )
    assert done["ok"] is True
    assert calls and calls[0][3] is True


@pytest.mark.asyncio
async def test_never_send_marker_blocks_the_provider_call(tmp_path: Path) -> None:
    provider = ScriptedProvider([_turn(_finish("should not run"))])
    result = await run_agent(
        "read my [never_send_to_model] note", root=tmp_path, provider=provider,
        limits=AgentLimits(max_steps=3, max_tool_calls=3, max_seconds=60),
    )
    assert result.error == "privacy_boundary"
    assert provider.seen == []


@pytest.mark.asyncio
async def test_secret_in_tool_output_is_redacted_before_the_next_call(tmp_path: Path) -> None:
    (tmp_path / "creds.txt").write_text(
        "api_key = sk-abcdefghijklmnopqrstuvwxyz\n", encoding="utf-8"
    )
    provider = ScriptedProvider([
        _turn(ToolCall(id="c1", name="read_file", arguments={"path": "creds.txt"})),
        _turn(_finish("read it")),
    ])
    result = await run_agent(
        "read creds.txt", root=tmp_path, provider=provider,
        limits=AgentLimits(max_steps=4, max_tool_calls=4, max_seconds=60),
    )
    assert result.ok is True
    second_call = provider.seen[1]
    tool_messages = [msg for msg in second_call if msg.role == "tool"]
    assert tool_messages, "the tool receipt must be fed back"
    blob = "".join(msg.content for msg in tool_messages)
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in blob
    assert "[credential redacted]" in blob


@pytest.mark.asyncio
async def test_shell_cwd_outside_root_parks_for_approval(tmp_path: Path) -> None:
    outside = tmp_path.parent
    result = await agent_tools.REGISTRY["run_shell"].run(
        {"cmd": "pwd", "cwd": str(outside)}, ToolContext(root=tmp_path, approved=False)
    )
    assert result["ok"] is False
    assert result["needs_confirm"] is True


# --------------------------------------------------------------------------- #
# Vision + terminal control
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_read_image_ocrs_a_local_file(tmp_path: Path) -> None:
    shot = tmp_path / "shot.txt"
    shot.write_text("ghostty session: opencode resume --session 42\n", encoding="utf-8")
    result = await agent_tools.REGISTRY["read_image"].run(
        {"path": "shot.txt"}, ToolContext(root=tmp_path, approved=True)
    )
    assert result["ok"] is True
    payload = result["result"]
    assert "opencode resume" in (payload.get("ocr") or payload.get("text") or "")


@pytest.mark.asyncio
async def test_capture_screen_uses_the_bridge_and_ocrs(monkeypatch, tmp_path: Path) -> None:
    shot = tmp_path / "screen.txt"
    shot.write_text("Terminal — evie@mac % echo hello\nhello\n", encoding="utf-8")
    import app.agent.local_tools as local_tools

    monkeypatch.setattr(
        local_tools, "_capture_sync",
        lambda: {"ok": True, "path": str(shot), "bytes": shot.stat().st_size,
                 "source": "test", "info": ""},
    )
    result = await agent_tools.REGISTRY["capture_screen"].run(
        {}, ToolContext(root=tmp_path, approved=True)
    )
    assert result["ok"] is True
    assert "echo hello" in (result["result"].get("ocr") or "")


@pytest.mark.asyncio
async def test_run_in_terminal_types_and_presses_return(monkeypatch, tmp_path: Path) -> None:
    scripts: list[str] = []
    monkeypatch.setattr(agent_tools, "_bridge_available", lambda: True)
    monkeypatch.setattr(
        agent_tools, "_open_sync", lambda argv: {"ok": True, "verified": True, "result": {}}
    )
    monkeypatch.setattr(
        agent_tools, "_jxa",
        lambda script, timeout=45.0: scripts.append(script) or {"ok": True, "verified": True},
    )
    result = await agent_tools.REGISTRY["run_in_terminal"].run(
        {"app": "Ghostty", "command": "opencode resume --session 42", "wait_s": 0},
        ToolContext(root=tmp_path, approved=True),
    )
    assert result["ok"] is True
    assert result["result"]["pressed_return"] is True
    assert scripts and "opencode resume --session 42" in scripts[0]
    assert "keyCode(36)" in scripts[0]
    assert 'Application("Ghostty")' in scripts[0]


@pytest.mark.asyncio
async def test_run_in_terminal_requires_approval_and_blocks_deny(tmp_path: Path) -> None:
    refused = await agent_tools.REGISTRY["run_in_terminal"].run(
        {"app": "Ghostty", "command": "echo hi"},
        ToolContext(root=tmp_path, approved=False),
    )
    assert refused["needs_confirm"] is True

    denied = await agent_tools.REGISTRY["run_in_terminal"].run(
        {"app": "Ghostty", "command": "sudo rm -rf /"},
        ToolContext(root=tmp_path, approved=True),
    )
    assert denied["ok"] is False
    assert "denied" in (denied["error"] or "")


@pytest.mark.asyncio
async def test_press_key_maps_return_to_key_code(monkeypatch, tmp_path: Path) -> None:
    scripts: list[str] = []
    monkeypatch.setattr(agent_tools, "_bridge_available", lambda: True)
    monkeypatch.setattr(
        agent_tools, "_jxa",
        lambda script, timeout=45.0: scripts.append(script) or {"ok": True},
    )
    result = await agent_tools.REGISTRY["press_key"].run(
        {"app": "Ghostty", "key": "return"},
        ToolContext(root=tmp_path, approved=True),
    )
    assert result["ok"] is True
    assert "keyCode(36)" in scripts[0]


# --------------------------------------------------------------------------- #
# File mutations (append / edit / move / copy)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_append_and_edit_verify_the_result(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("alpha\n", encoding="utf-8")
    ctx = ToolContext(root=tmp_path, approved=True)
    appended = await agent_tools.REGISTRY["append_file"].run(
        {"path": "note.txt", "content": "beta\n"}, ctx
    )
    assert appended["ok"] and appended["verified"]
    assert (tmp_path / "note.txt").read_text() == "alpha\nbeta\n"

    edited = await agent_tools.REGISTRY["edit_file"].run(
        {"path": "note.txt", "find": "beta", "replace": "gamma"}, ctx
    )
    assert edited["ok"] and edited["result"]["replacements"] == 1
    assert (tmp_path / "note.txt").read_text() == "alpha\ngamma\n"

    missing = await agent_tools.REGISTRY["edit_file"].run(
        {"path": "note.txt", "find": "nothing", "replace": "x"}, ctx
    )
    assert missing["ok"] is False
    assert (tmp_path / "note.txt").read_text() == "alpha\ngamma\n"


@pytest.mark.asyncio
async def test_move_and_copy_paths(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("payload", encoding="utf-8")
    ctx = ToolContext(root=tmp_path, approved=True)

    copied = await agent_tools.REGISTRY["copy_path"].run(
        {"source": "a.txt", "destination": "copies/a.txt"}, ctx
    )
    assert copied["ok"] and copied["verified"]
    assert (tmp_path / "copies" / "a.txt").read_text() == "payload"
    assert (tmp_path / "a.txt").exists()

    moved = await agent_tools.REGISTRY["move_path"].run(
        {"source": "a.txt", "destination": "moved.txt"}, ctx
    )
    assert moved["ok"] and moved["verified"]
    assert (tmp_path / "moved.txt").read_text() == "payload"
    assert not (tmp_path / "a.txt").exists()

    collision = await agent_tools.REGISTRY["move_path"].run(
        {"source": "moved.txt", "destination": "copies/a.txt"}, ctx
    )
    assert collision["ok"] is False


@pytest.mark.asyncio
async def test_sql_query_runs_ddl_dml_and_select(tmp_path: Path) -> None:
    ctx = ToolContext(root=tmp_path, approved=True)
    db = "scratch.db"
    created = await agent_tools.REGISTRY["sql_query"].run(
        {"sql": "CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)", "db_path": db}, ctx
    )
    assert created["ok"], created
    inserted = await agent_tools.REGISTRY["sql_query"].run(
        {"sql": "INSERT INTO items (name) VALUES ('alpha')", "db_path": db}, ctx
    )
    assert inserted["ok"], inserted
    selected = await agent_tools.REGISTRY["sql_query"].run(
        {"sql": "SELECT name FROM items", "db_path": db}, ctx
    )
    assert selected["ok"]
    assert ("alpha",) in [tuple(row) for row in selected["result"]["rows"]]
