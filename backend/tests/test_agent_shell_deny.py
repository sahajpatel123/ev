"""Shell denial receipts name the fired rule class (live-evidence fix).

A disk-check run burned 6 turns retrying ~/Library variants because every
denial said only "denied by policy". Policy is unchanged — the lane still
decides — but the receipt now tells the brain which class fired and what to
do instead. Hermetic: denials never execute; the control echoes locally.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.tools import REGISTRY, ToolContext


def _ctx(root: Path) -> ToolContext:
    return ToolContext(root=root, approved=True, run_id="deny-test")


@pytest.mark.asyncio
async def test_sudo_denial_names_privilege_and_runs_nothing(tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    out = await REGISTRY["run_shell"].run({"cmd": f"sudo touch {marker}"}, _ctx(tmp_path))
    assert out["ok"] is False
    assert "privilege escalation" in str(out["error"])
    assert "never allowed" in str(out["error"])
    assert not marker.exists()


@pytest.mark.asyncio
async def test_library_denial_names_secret_path(tmp_path: Path) -> None:
    out = await REGISTRY["run_shell"].run(
        {"cmd": "du -sh ~/Library/* 2>/dev/null | sort -rh | head -5"}, _ctx(tmp_path)
    )
    assert out["ok"] is False
    assert "secret path" in str(out["error"])
    assert "parent directory" in str(out["error"])


@pytest.mark.asyncio
async def test_destructive_denial_names_pattern(tmp_path: Path) -> None:
    out = await REGISTRY["run_shell"].run({"cmd": "rm -rf /"}, _ctx(tmp_path))
    assert out["ok"] is False
    assert "destructive" in str(out["error"])


@pytest.mark.asyncio
async def test_allowed_command_still_runs(tmp_path: Path) -> None:
    out = await REGISTRY["run_shell"].run({"cmd": "echo hi"}, _ctx(tmp_path))
    assert out["ok"] is True
    assert "hi" in str((out.get("result") or {}).get("stdout") or "")


@pytest.mark.asyncio
async def test_loop_level_deny_names_rule_class(tmp_path: Path) -> None:
    from app.agent.loop import AgentLimits, run_agent
    from app.contracts import ChatResult, ToolCall

    turns = [
        ChatResult(
            text="",
            tool_calls=[ToolCall(id="c1", name="run_shell", arguments={"cmd": "sudo du -sh /"})],
            usage={},
            model="t",
        ),
        ChatResult(
            text="",
            tool_calls=[ToolCall(id="f1", name="finish", arguments={"summary": "stopped", "ok": False})],
            usage={},
            model="t",
        ),
    ]

    class _Stub:
        name = "t"

        async def chat_with_tools(self, messages, tools, *, model=None, temperature=0.7):
            return turns.pop(0)

    out = await run_agent(
        "probe denial",
        root=tmp_path,
        provider=_Stub(),
        approved=True,
        limits=AgentLimits(max_steps=4, max_tool_calls=4, max_seconds=60),
    )
    assert any("privilege escalation" in str(step.error or "") for step in out.steps)
