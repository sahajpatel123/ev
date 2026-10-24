"""R7 / D4: the risk floor.

Sends, calls, SQL writes, permanent deletes, money, credentials and first
contact always ask, whatever the owner's autonomy setting says. An approval
covers only the action that was approved, and a spoken "yes" never approves
one of these. Only a physical confirmation does: the HUD card, the paired
phone, or a typed reply on an authenticated surface.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.agent import tools as agent_tools
from app.agent.loop import AgentLimits, ApprovalRequest, _classify, run_agent
from app.config import settings
from app.contracts import ChatResult, ToolCall
from app.fabric import grants
from app.fabric.policy import HIGH_RISK, physical_confirm_required, risk_class

# --------------------------------------------------------------------------- #
# risk_class: the one table that names a high-risk call
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("tool", "args", "expected"),
    [
        ("life.send", {"to": "sam", "text": "hi"}, "send"),
        ("phone.call", {"op": "call", "contact": "sam"}, "call"),
        ("phone.call", {"op": "message", "contact": "sam", "message": "hi"}, "send"),
        ("comms.act", {"op": "send", "to": "sam", "body": "hi"}, "send"),
        ("comms.act", {"op": "draft", "to": "sam", "body": "hi"}, None),
        ("digital.act", {"service": "gmail", "operation": "send"}, "send"),
        ("digital.act", {"service": "gmail", "operation": "reply"}, "send"),
        ("digital.act", {"service": "gmail", "operation": "trash"}, "delete"),
        ("digital.act", {"service": "gmail", "operation": "search"}, None),
        ("sql_query", {"sql": "DELETE FROM notes"}, "sql_write"),
        ("sql_query", {"sql": "DROP TABLE notes"}, "sql_write"),
        ("sql_query", {"sql": "SELECT * FROM notes"}, None),
        ("sql.query", {"sql": "UPDATE t SET a = 1"}, "sql_write"),
        ("run_shell", {"cmd": "rm -rf build"}, "delete"),
        ("run_shell", {"cmd": "git push origin main"}, "publish"),
        ("run_shell", {"cmd": "curl -X POST https://example.com -d @notes.txt"}, "egress"),
        ("run_shell", {"cmd": "scp notes.txt host:/tmp"}, "egress"),
        ("run_shell", {"cmd": "pytest -q"}, None),
        ("run_shell", {"cmd": "ls -la"}, None),
        ("shell.run", {"command": "rm notes.txt"}, "delete"),
        ("terminal_run", {"command": "git reset --hard HEAD~1"}, "delete"),
        ("run_applescript", {"script": 'tell application "Messages" to send "hi" to buddy "Sam"'}, "send"),
        ("run_applescript", {"script": 'tell application "Finder" to empty trash'}, "delete"),
        ("run_applescript", {"script": 'tell application "Finder" to get name of front window'}, None),
        ("mac.act", {"op": "jxa", "script": "Application('Mail').send(msg)"}, "send"),
        ("pim.act", {"op": "note_delete", "id": "n1"}, "delete"),
        ("pim.act", {"op": "reminder_create", "title": "milk"}, None),
        ("write_file", {"path": "notes.txt", "content": "x"}, None),
        ("delete_path", {"path": "notes.txt"}, None),
    ],
)
def test_risk_class_table(tool: str, args: dict, expected: str | None) -> None:
    spec = agent_tools.REGISTRY[tool].spec
    assert risk_class(spec.name, str(getattr(spec, "permission", "")), args) == expected


def test_physical_confirm_covers_every_high_risk_class() -> None:
    for cls in HIGH_RISK:
        assert physical_confirm_required(cls)
    assert not physical_confirm_required(None)


# --------------------------------------------------------------------------- #
# _classify: autonomy cannot lower the floor
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _no_grants():
    grants.revoke_all()
    yield
    grants.revoke_all()


def test_trusted_autonomy_still_confirms_a_send(monkeypatch) -> None:
    monkeypatch.setattr(settings, "agent_autonomy", "trusted", raising=False)
    tool = agent_tools.REGISTRY["life.send"]
    assert _classify(tool, {"to": "sam", "text": "hi"}, approved=True) == "CONFIRM"


def test_trusted_autonomy_still_confirms_a_call_and_a_sql_write() -> None:
    call = agent_tools.REGISTRY["phone.call"]
    assert _classify(call, {"op": "call", "contact": "sam"}, approved=True) == "CONFIRM"
    sql = agent_tools.REGISTRY["sql_query"]
    assert _classify(sql, {"sql": "DELETE FROM t"}, approved=True) == "CONFIRM"


def test_destructive_shell_confirms_even_when_approved() -> None:
    tool = agent_tools.REGISTRY["run_shell"]
    assert _classify(tool, {"cmd": "rm -rf build"}, approved=True) == "CONFIRM"
    assert _classify(tool, {"cmd": "git push origin main"}, approved=True) == "CONFIRM"


def test_ordinary_work_is_not_slowed_down() -> None:
    """The floor is narrow: builds, tests and file writes keep running."""
    assert _classify(agent_tools.REGISTRY["run_shell"], {"cmd": "pytest -q"}, approved=True) == "AUTO"
    assert _classify(
        agent_tools.REGISTRY["write_file"], {"path": "a.txt", "content": "x"}, approved=True
    ) == "AUTO"


def test_a_standing_grant_lifts_the_send_floor() -> None:
    tool = agent_tools.REGISTRY["life.send"]
    assert grants.grant("send", ttl_s=60)["ok"]
    assert _classify(tool, {"to": "sam", "text": "hi"}, approved=True) == "AUTO"
    grants.revoke("send")
    assert _classify(tool, {"to": "sam", "text": "hi"}, approved=True) == "CONFIRM"


def test_an_expired_grant_does_not_lift_the_floor() -> None:
    tool = agent_tools.REGISTRY["life.send"]
    grants.grant("send", ttl_s=-1)
    assert _classify(tool, {"to": "sam", "text": "hi"}, approved=True) == "CONFIRM"


def test_secret_paths_stay_denied_above_the_floor() -> None:
    tool = agent_tools.REGISTRY["files.act"]
    assert _classify(tool, {"op": "read", "path": "~/.ssh/id_rsa.key"}, approved=True) == "DENY"


# --------------------------------------------------------------------------- #
# The loop: one approval, one action
# --------------------------------------------------------------------------- #


class _ScriptedProvider:
    name = "scripted"

    def __init__(self, turns: list[ChatResult]) -> None:
        self._turns = list(turns)

    async def chat_with_tools(self, messages, tools, *, model=None, temperature=0.7, **kw):
        if self._turns:
            return self._turns.pop(0)
        return ChatResult(text="nothing left to do", model="scripted")


class _CountingGate:
    def __init__(self, decision: str) -> None:
        self.decision = decision
        self.requests: list[ApprovalRequest] = []

    async def request(self, request: ApprovalRequest) -> str:
        self.requests.append(request)
        return self.decision


def _send(call_id: str, text: str) -> ToolCall:
    return ToolCall(id=call_id, name="life.send", arguments={"to": "sam", "text": text})


async def test_approve_run_on_a_send_covers_only_that_send(tmp_path, monkeypatch) -> None:
    sent: list[dict] = []

    async def fake_run(arguments, ctx):
        sent.append(dict(arguments))
        return agent_tools._receipt(ok=True, verified=True, result={"sent": True})

    monkeypatch.setitem(agent_tools.REGISTRY, "life.send",
                        dataclasses.replace(agent_tools.REGISTRY["life.send"], run=fake_run))
    provider = _ScriptedProvider([
        ChatResult(text="", tool_calls=[_send("s1", "first")], usage={}, model="scripted"),
        ChatResult(text="", tool_calls=[_send("s2", "second")], usage={}, model="scripted"),
        ChatResult(text="", tool_calls=[ToolCall(id="f", name="finish", arguments={"summary": "sent"})],
                   usage={}, model="scripted"),
    ])
    gate = _CountingGate("approve_run")
    result = await run_agent(
        "send two messages", root=tmp_path, provider=provider, approved=True, gate=gate,
        limits=AgentLimits(max_steps=6, max_tool_calls=6, max_seconds=30),
    )
    assert [s["text"] for s in sent] == ["first", "second"]
    assert len(gate.requests) == 2, "each send needs its own approval"
    assert all(r.risk == "send" for r in gate.requests)
    assert all(r.physical_required for r in gate.requests)
    assert result.status == "completed"


async def test_a_denied_send_does_not_happen(tmp_path, monkeypatch) -> None:
    sent: list[dict] = []

    async def fake_run(arguments, ctx):
        sent.append(dict(arguments))
        return agent_tools._receipt(ok=True, verified=True)

    monkeypatch.setitem(agent_tools.REGISTRY, "life.send",
                        dataclasses.replace(agent_tools.REGISTRY["life.send"], run=fake_run))
    provider = _ScriptedProvider([
        ChatResult(text="", tool_calls=[_send("s1", "hello")], usage={}, model="scripted"),
        ChatResult(text="", tool_calls=[ToolCall(id="f", name="finish",
                                                 arguments={"summary": "not sent", "ok": False})],
                   usage={}, model="scripted"),
    ])
    gate = _CountingGate("deny")
    await run_agent(
        "send hello", root=tmp_path, provider=provider, approved=True, gate=gate,
        limits=AgentLimits(max_steps=4, max_tool_calls=4, max_seconds=30),
    )
    assert sent == []
    assert len(gate.requests) == 1


# --------------------------------------------------------------------------- #
# Voice cannot approve a high-risk action; typed and the decision API can
# --------------------------------------------------------------------------- #


def test_voice_yes_is_refused_for_high_risk_but_typed_is_accepted() -> None:
    from app.agent.intake import approval_channel_allows

    send_row = {"approval_tool": "life.send", "approval_args": {"to": "sam", "text": "hi"}}
    write_row = {"approval_tool": "write_file", "approval_args": {"path": "a.txt"}}
    assert approval_channel_allows(send_row, source="voice") is False
    assert approval_channel_allows(send_row, source="hands_free") is False
    assert approval_channel_allows(send_row, source="chat") is True
    assert approval_channel_allows(send_row, source="typed") is True
    assert approval_channel_allows(write_row, source="voice") is True


async def test_voice_yes_on_a_pending_send_does_not_approve(monkeypatch) -> None:
    from app.agent import intake, runner

    pending = {
        "id": "task-1", "run_id": "run-1", "goal": "send sam hi",
        "status": "awaiting_approval", "approval_tool": "life.send",
        "approval_args": {"to": "sam", "text": "hi"}, "pid": 0,
    }
    decisions: list[tuple[str, str]] = []
    monkeypatch.setattr(runner, "pending_approval", lambda: pending)
    monkeypatch.setattr(runner, "approve_task", lambda tid, d="approve_run": decisions.append((tid, d)))
    monkeypatch.setattr(intake, "_live", lambda row: True)
    out = await intake.handle_control("yes", source="voice")
    assert decisions == []
    assert out is not None
    spoken = out["spoken"].lower()
    assert "confirm" in spoken and ("phone" in spoken or "return" in spoken)
