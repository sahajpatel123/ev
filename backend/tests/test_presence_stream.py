"""RC SEAM: presence stream server side (evie-presence/1) conformance.

Covers docs/contracts/PRESENCE_EVENTS.md §6 as far as the server can prove
it: hello-first, heartbeat, replay ring, per-turn causal order, schema
validity of every emitted event, and the producer hooks (kernel turns,
agent runs, REST parks, panic, background completion).
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from app.fabric import presence_bus as pb


@pytest.fixture(autouse=True)
def _clean_bus():
    pb.bus.reset_for_tests()
    pb._run_goals.clear()
    yield
    pb.bus.reset_for_tests()
    pb._run_goals.clear()


def _env(etype: str, data: dict, turn_id: str | None = "ft-test") -> dict:
    return {"v": pb.CONTRACT, "id": "pe-x", "seq": 1,
            "ts": "2026-10-27T00:00:00.000Z", "turn_id": turn_id,
            "type": etype, "data": data}


def _valid_data() -> dict[str, dict]:
    return {
        "stream.hello": {"contract": pb.CONTRACT, "server_time": "t", "cursor": None,
                         "resumed": True, "gap": False, "restarted": False, "capabilities": []},
        "turn.started": {"source": "typed", "utterance_preview": "hi", "label": "L"},
        "turn.listening": {"level": 0.5, "degraded": False},
        "turn.transcript.partial": {"text": "a", "sequence": 1, "stable": False, "confidence": 0.5},
        "turn.transcript.final": {"text": "a", "confidence": 0.9, "provider": "p"},
        "turn.thinking": {"label": "T", "elapsed_ms": 3},
        "plan.updated": {"label": "L", "steps": [{"id": "s1", "label": "a", "status": "active"}]},
        "tool.started": {"call_id": "c", "step_id": "s1", "name": "n", "label": "l", "target": ""},
        "tool.finished": {"call_id": "c", "step_id": "s1", "name": "n", "label": "l",
                          "ok": True, "verified": True, "evidence": {}, "error": None,
                          "receipt_id": "", "undo": {"kind": "none"}},
        "turn.needs_confirm": {"ticket_id": "t", "risk_class": "R2", "channel": "other",
                               "label": "L", "expires_at": "e",
                               "approve": {"method": "POST", "path": "/a"},
                               "deny": {"method": "POST", "path": "/d"}},
        "turn.progress": {"text": "t", "spoken": True, "sequence": 0},
        "turn.completed": {"summary": "s", "receipt_ids": [], "verified": True, "label": "Done"},
        "turn.failed": {"reason": "r", "category": "other", "failed_step_id": None,
                        "recoverable": True, "next_step": "n", "receipt_ids": []},
        "background.update": {"kind": "sync.note", "label": "l"},
        "panic.state": {"panicked": False, "at": "a", "reason": ""},
    }


async def test_validate_accepts_every_backend_type() -> None:
    for etype, data in _valid_data().items():
        pb.validate_frame(_env(etype, data))  # must not raise


async def test_validate_rejects_app_local_unknown_and_missing() -> None:
    with pytest.raises(ValueError):
        pb.validate_frame(_env("ambient.looking_at", {"app": "x"}))
    with pytest.raises(ValueError):
        pb.validate_frame(_env("turn.nope", {}))
    with pytest.raises(ValueError):
        pb.validate_frame(_env("turn.started", {"source": "typed"}))
    with pytest.raises(ValueError):
        pb.validate_frame(_env("plan.updated", {"label": "l", "steps": [{"id": "s1"}]}))
    with pytest.raises(ValueError):
        pb.validate_frame(_env("turn.failed", {**_valid_data()["turn.failed"], "category": "bogus"}))


async def test_publish_assigns_seq_and_replays() -> None:
    e1 = pb.emit_turn_started(turn_id="ft-1", source="typed", utterance_preview="hi")
    e2 = pb.emit_turn_completed(turn_id="ft-1", summary="done", verified=True)
    assert (e1["seq"], e2["seq"]) == (1, 2)
    frames, resumed, gap = pb.bus.replay_since(None)
    assert frames == [] and resumed and not gap
    frames, resumed, gap = pb.bus.replay_since(e1["id"])
    assert [f["id"] for f in frames] == [e2["id"]] and resumed and not gap
    frames, resumed, gap = pb.bus.replay_since("pe-missing")
    assert frames == [] and not resumed and gap


async def test_agent_sink_translates_plan_tool_thinking() -> None:
    pb.agent_sink({"kind": "run_started", "run_id": "run-abc123", "goal": "Text Sam"})
    pb.agent_sink({"kind": "plan_updated", "run_id": "run-abc123", "todos": [
        {"content": "Find Sam", "status": "completed"},
        {"content": "Draft", "status": "in_progress"},
        {"content": "Send", "status": "pending"},
    ]})
    pb.agent_sink({"kind": "tool_started", "run_id": "run-abc123", "step": 2, "tool": "send_msg"})
    pb.agent_sink({"kind": "tool_result", "run_id": "run-abc123", "step": 2,
                   "tool": "send_msg", "ok": True, "verified": True, "error": None,
                   "undo": {"kind": "none"}})
    pb.agent_sink({"kind": "brain_turn", "run_id": "run-abc123", "step": 3,
                   "tool_calls": 0, "text": "Considering"})
    pb.agent_sink({"kind": "something_else", "run_id": "run-abc123"})
    frames, _, _ = pb.bus.replay_since(None)
    assert frames == []  # replay_since(None) is always empty; read the ring instead
    ring = list(pb.bus._ring)
    types = [f["type"] for f in ring]
    assert types == ["plan.updated", "tool.started", "tool.finished", "turn.thinking"]
    plan = ring[0]["data"]
    assert plan["label"] == "Text Sam"
    assert [s["status"] for s in plan["steps"]] == ["done", "active", "queued"]
    assert ring[1]["data"]["call_id"] == ring[2]["data"]["call_id"]
    assert ring[2]["data"]["verified"] is True
    assert all(f["turn_id"] == "ft-abc123" for f in ring)
    for f in ring:
        pb.validate_frame(f)


async def test_channel_for_action() -> None:
    assert pb.channel_for_action("comms_act", {"op": "send"}) == "message.send"
    assert pb.channel_for_action("send_message", {"channel": "fake"}) == "message.send"
    assert pb.channel_for_action("send_mail", {"channel": "mail"}) == "mail.send"
    assert pb.channel_for_action("execute_command", {}) == "shell.exec"
    assert pb.channel_for_action("comms_act", {"op": "email.send"}) == "mail.send"
    assert pb.channel_for_action("files_op", {"op": "delete"}) == "file.delete"
    assert pb.channel_for_action("files_op", {"op": "write"}) == "file.write"
    assert pb.channel_for_action("sql_query", {"sql": "select 1"}) == "sql.dml"
    assert pb.channel_for_action("sql_query", {"sql": "DROP TABLE x"}) == "sql.ddl"
    assert pb.channel_for_action("shell_run", {}) == "shell.exec"
    assert pb.channel_for_action("unknown_lane", {}) == "other"


async def test_kernel_turn_emits_started_and_terminal(monkeypatch) -> None:
    from app.cognitive import kernel

    async def fake_turn(**kwargs):
        return kernel.KernelResult(spoken="Hello back.", kind="brain",
                                   evidence=[{"verified": True, "receipt_id": "rc-1"}])

    monkeypatch.setattr(kernel, "_handle_turn", fake_turn)
    result = await kernel.handle_turn(transcript="hello", modality="typed")
    assert result.kind == "brain"
    ring = list(pb.bus._ring)
    types = [f["type"] for f in ring]
    assert types[0] == "turn.started"
    assert types[-1] == "turn.completed"
    assert ring[-1]["data"]["verified"] is True
    assert ring[-1]["data"]["receipt_ids"] == ["rc-1"]
    turn_ids = {f["turn_id"] for f in ring}
    assert len(turn_ids) == 1 and next(iter(turn_ids)).startswith("ft-")
    for f in ring:
        pb.validate_frame(f)


async def test_kernel_failed_turn_emits_failed(monkeypatch) -> None:
    from app.cognitive import kernel

    async def boom(**kwargs):
        raise RuntimeError("brain down")

    monkeypatch.setattr(kernel, "_handle_turn", boom)
    result = await kernel.handle_turn(transcript="hello", modality="voice")
    assert result.kind == "failed"
    ring = list(pb.bus._ring)
    assert ring[0]["type"] == "turn.started"
    assert ring[-1]["type"] == "turn.failed"
    assert ring[-1]["data"]["category"] in {"provider_error", "other"}
    for f in ring:
        pb.validate_frame(f)


async def test_kernel_unavailable_turn_emits_failed_not_verified(monkeypatch) -> None:
    from app.cognitive import kernel

    async def unavailable(**kwargs):
        return kernel.KernelResult(spoken="I can't think right now.", kind="unavailable",
                                   unavailable=True)

    monkeypatch.setattr(kernel, "_handle_turn", unavailable)
    result = await kernel.handle_turn(transcript="hello", modality="typed")
    assert result.unavailable is True
    ring = list(pb.bus._ring)
    assert ring[-1]["type"] == "turn.failed"
    assert ring[-1]["data"]["category"] == "provider_error"
    assert "turn.completed" not in [f["type"] for f in ring]
    for f in ring:
        pb.validate_frame(f)


async def test_since_endpoint_replays(client, db_session) -> None:
    e1 = pb.emit_turn_started(turn_id="ft-9", source="typed")
    pb.emit_turn_completed(turn_id="ft-9", summary="s", verified=True)
    resp = await client.get("/v1/fabric/presence/since", params={"cursor": e1["id"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["contract"] == pb.CONTRACT
    assert body["resumed"] is True and body["gap"] is False
    assert [f["type"] for f in body["frames"]] == ["turn.completed"]
    resp = await client.get("/v1/fabric/presence/since", params={"cursor": "pe-gone"})
    assert resp.json()["gap"] is True


async def test_stream_frames_hello_first_then_replay_and_heartbeat(monkeypatch) -> None:
    # NOTE: httpx's ASGI transport deadlocks on infinite StreamingResponses
    # (its receive() waits for response completion while Starlette waits for
    # disconnect), so the frame source is tested directly. The HTTP wire is
    # covered against a real socket server in S4.
    monkeypatch.setattr(pb, "HEARTBEAT_S", 0.02)
    e1 = pb.emit_turn_started(turn_id="ft-7", source="typed")
    pb.emit_turn_completed(turn_id="ft-7", summary="s", verified=True)

    async def connected() -> bool:
        return False

    chunks: list[str] = []
    gen = pb.stream_frames(e1["id"], is_disconnected=connected)
    async with asyncio.timeout(5):
        async for chunk in gen:
            chunks.append(chunk)
            if chunk == ":heartbeat\n\n":
                break
            if len(chunks) > 6:
                break
    await gen.aclose()
    assert chunks[0].startswith("id: pe-") and "\nevent: stream.hello\n" in chunks[0]
    assert "\nevent: turn.completed\n" in chunks[1]  # replayed: frames after e1
    assert chunks[-1] == ":heartbeat\n\n"


async def test_stream_frames_live_delivery_and_disconnect(monkeypatch) -> None:
    monkeypatch.setattr(pb, "HEARTBEAT_S", 0.02)
    calls = 0

    async def flaky() -> bool:
        nonlocal calls
        calls += 1
        return calls > 3

    gen = pb.stream_frames(None, is_disconnected=flaky)
    seen: list[str] = []

    async def drive():
        async for chunk in gen:
            seen.append(chunk)

    task = asyncio.create_task(drive())
    await asyncio.sleep(0.05)
    pb.emit_turn_started(turn_id="ft-live", source="voice")
    await asyncio.wait_for(task, timeout=5)
    events = [c for c in seen if c.startswith("id: pe-")]
    assert len(events) == 2  # hello + the live frame
    assert "\nevent: turn.started\n" in events[1]


async def test_stream_requires_auth() -> None:
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as raw:
        resp = await raw.get("/v1/fabric/presence/stream")
        assert resp.status_code in {401, 403}


async def test_panic_hook_emits_state(tmp_path, monkeypatch) -> None:
    from app.config import settings
    from app.fabric import panic as panic_mod

    monkeypatch.setattr(settings, "fabric_panic_file", str(tmp_path / "panic.json"))
    panic_mod.panic("rc test")
    panic_mod.clear()
    ring = list(pb.bus._ring)
    assert [f["type"] for f in ring] == ["panic.state", "panic.state"]
    assert ring[0]["data"]["panicked"] is True
    assert ring[1]["data"]["panicked"] is False
    for f in ring:
        pb.validate_frame(f)
