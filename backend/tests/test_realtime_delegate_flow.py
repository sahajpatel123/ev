"""Realtime-first delegation: Mini fronts, MiMo works, results return.

Hermetic and offline. The fake kernel stands in for the MiMo worker so the
suite proves the *handoff contract* (receipt now, result later) without a
network call: no test may depend on OpenRouter or OpenAI being reachable.
"""

from __future__ import annotations

import asyncio

from app.config import settings


def _delegate_mode(monkeypatch) -> None:
    monkeypatch.setattr(settings, "cognitive_mode", "realtime_delegate")
    monkeypatch.setattr(settings, "mimo_model", "xiaomi/mimo-v2.6-flash")
    monkeypatch.setattr(settings, "openai_realtime_model", "gpt-realtime-2.1-mini")


def test_delegate_mode_flags(monkeypatch) -> None:
    from app.cognitive import mode as mode_mod

    _delegate_mode(monkeypatch)
    assert mode_mod.realtime_delegate_active() is True
    assert mode_mod.mimo_kernel_active() is False
    assert mode_mod.kernel_mode_active() is False


def test_delegate_task_spec_is_a_single_task_contract() -> None:
    from app.cognitive.delegation import delegate_task_spec

    spec = delegate_task_spec()
    assert spec["type"] == "function"
    assert spec["name"] == "delegate_task"
    params = spec["parameters"]
    assert "task" in params["properties"]
    assert params["properties"]["task"]["type"] == "string"


def test_grok_voice_advertises_only_the_delegate_tool(monkeypatch) -> None:
    from app.voice.live.grok_voice import grok_voice_tools

    _delegate_mode(monkeypatch)
    tools = grok_voice_tools(
        [{"name": "search_memory", "parameters": {"type": "object", "properties": {}}}]
    )
    assert [tool["name"] for tool in tools] == ["delegate_task"]


def test_instructions_promise_a_receipt_never_completion(monkeypatch) -> None:
    from app.voice.live.grok_voice import realtime_delegate_instructions

    _delegate_mode(monkeypatch)
    text = realtime_delegate_instructions()
    lowered = text.lower()
    assert "delegate_task" in text
    assert "accepted" in lowered
    assert "queued" in lowered or "receipt" in lowered
    assert "never" in lowered


def test_non_speech_roles_stay_on_mimo_in_delegate_mode(monkeypatch) -> None:
    from app.gateway.roles import resolve_code_brain, resolve_text_brain, text_brain_active

    _delegate_mode(monkeypatch)
    assert resolve_text_brain().provider == "mimo"
    assert resolve_text_brain().model == "xiaomi/mimo-v2.6-flash"
    assert resolve_code_brain().provider == "mimo"
    assert text_brain_active() is True


def test_phone_public_reports_realtime_brain_and_mimo_worker(monkeypatch) -> None:
    from app.device_gateway.webrtc_live import phone_cognitive_public

    _delegate_mode(monkeypatch)
    public = phone_cognitive_public()
    assert public["mode"] == "realtime_delegate"
    assert public["brain"] == "gpt-realtime-2.1-mini"
    assert public["delegated_worker"] == "xiaomi/mimo-v2.6-flash"
    assert public["realtime_thinks"] is True


async def test_submit_delegate_receipts_then_delivers_worker_result(monkeypatch) -> None:
    from app.cognitive import delegation
    from app.cognitive.kernel import KernelResult

    _delegate_mode(monkeypatch)
    gate = asyncio.Event()
    seen: list[str] = []

    async def fake_handle_turn(**kwargs):
        seen.append(kwargs["transcript"])
        await gate.wait()
        return KernelResult(spoken="The report is on your desk.", kind="mimo")

    monkeypatch.setattr("app.cognitive.kernel.handle_turn", fake_handle_turn)
    delivered = asyncio.Event()
    receipts: list[dict] = []

    async def on_complete(receipt: dict) -> None:
        receipts.append(receipt)
        delivered.set()

    receipt = await delegation.submit_delegate(
        task="Summarize my inbox",
        request_id="call-1",
        live_session_id="live-1",
        on_complete=on_complete,
    )
    assert receipt["accepted"] is True
    assert receipt["status"] == "queued"
    spoken = receipt["spoken"].lower()
    assert "task agent" in spoken or "let you know" in spoken

    # The same request id is the same durable job: no second inference.
    again = await delegation.submit_delegate(
        task="Summarize my inbox", request_id="call-1", live_session_id="live-1"
    )
    assert again["job_id"] == receipt["job_id"]

    gate.set()
    await asyncio.wait_for(delivered.wait(), 5)
    assert seen == ["Summarize my inbox"]

    final = await delegation.get_delegate(receipt["job_id"])
    assert final is not None
    assert final["status"] == "answered"
    assert final["spoken"] == "The report is on your desk."
    assert receipts and receipts[0]["status"] == "answered"


async def test_submit_delegate_is_refused_outside_delegate_mode(monkeypatch) -> None:
    from app.cognitive import delegation

    monkeypatch.setattr(settings, "cognitive_mode", "mimo_kernel")
    receipt = await delegation.submit_delegate(
        task="Summarize my inbox", request_id="call-off"
    )
    assert receipt["accepted"] is False
    assert receipt["status"] == "failed"
