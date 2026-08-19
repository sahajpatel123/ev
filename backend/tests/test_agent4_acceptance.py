"""Agent 4 acceptance path: request -> advertised tool -> audited result.

These tests deliberately use the repository's local doubles.  The weather
provider is patched at its existing result seam, calendar data is ingested as
a local live event, and the phone adapter runs in ``simulate_opened`` mode.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.briefing import infer_args
from app.ev.tool_select import resolve_live_action
from app.ev.tools import get_spec
from app.ev.training_wheels import TRAINING_STEPS, complete_step
from app.models import AccessLog, ApprovedAction, OwnerTimer
from app.utils.text import utcnow
from app.voice.live.events import HudEvent, ReplyEvent
from app.voice.live.grok_voice import GrokVoiceBridge
from app.voice.live.layer import register_live, reset_live_registry, unregister_live
from app.voice.live.session import LiveSession
from app.voice.live.transport import _grok_tool_runner
from tests.test_gateway_xai import _acknowledge_session, _FakeRealtime


async def _install_local(
    client: AsyncClient,
    adapter: str,
    scopes: list[str],
    *,
    config: dict | None = None,
    slug: str | None = None,
) -> dict:
    response = await client.post(
        "/v1/integrations",
        json={
            "adapter": adapter,
            "slug": slug,
            "name": f"Agent 4 {adapter}",
            "scopes": scopes,
            "config": config or {"provider": "local"},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _unlock_training_wheels(db_session: AsyncSession) -> None:
    for step in TRAINING_STEPS:
        await complete_step(db_session, step)
    await db_session.commit()


def _drain(live: LiveSession) -> list:
    events = []
    while not live.outbound.empty():
        events.append(live.outbound.get_nowait())
    return events


async def _wait_for(predicate) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    assert predicate()


async def test_agent4_natural_requests_are_selected_advertised_and_shaped(
    client: AsyncClient,
) -> None:
    requests = (
        ("what's the weather in Surat?", "get_weather"),
        ("run diagnostics", "calibrate"),
        ("start a timer for 5 minutes", "start_timer"),
        ("what's on my calendar today?", "calendar_read"),
    )

    before = (await client.get("/v1/capabilities")).json()
    before_names = {item["name"] for item in before["tools"]}
    assert {"get_weather", "calibrate", "start_timer"} <= before_names
    assert "calendar_read" not in before_names

    for message, expected in requests:
        selected = await client.post("/v1/gateway/select-tool", json={"message": message})
        assert selected.status_code == 200, selected.text
        assert selected.json()["selected"] == expected

    weather_args = infer_args("get_weather", requests[0][0])
    assert weather_args == {
        "query": "what's the weather in Surat?",
        "place": "Surat",
    }
    assert resolve_live_action(requests[1][0]) == ("calibrate", {})
    assert resolve_live_action(requests[2][0]) == ("start_timer", {"minutes": 5})
    assert resolve_live_action(requests[3][0]) == ("calendar_read", {})
    assert get_spec("get_weather")["parameters"]["additionalProperties"] is False
    assert get_spec("calibrate")["parameters"]["additionalProperties"] is False

    await _install_local(client, "calendar", ["calendar:read"])
    after = (await client.get("/v1/capabilities")).json()
    after_names = {item["name"] for item in after["tools"]}
    assert "calendar_read" in after_names


async def test_agent4_local_reads_and_timer_produce_evidence_spoken_result_and_audit(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    weather_calls: list[tuple[str, int]] = []

    async def fake_weather_results(query: str, *, limit: int = 3, **_kwargs):
        weather_calls.append((query, limit))
        return [
            SimpleNamespace(
                title="Surat weather",
                url="https://local.test/weather/surat",
                snippet="Surat: clear sky, 28°C.",
            )
        ]

    monkeypatch.setattr("app.search.live.weather_results", fake_weather_results)
    weather_args = infer_args("get_weather", "what's the weather in Surat?")
    weather = await client.post(
        "/v1/gateway/tools",
        json={"name": "get_weather", "arguments": weather_args, "request_id": "agent4-weather"},
    )
    assert weather.status_code == 200, weather.text
    weather_body = weather.json()
    assert weather_body["ok"] is True
    assert weather_body["result"]["ok"] is True
    assert weather_body["result"]["results"][0]["url"] == "https://local.test/weather/surat"
    assert weather_body["result"]["evidence"]["source"] == "open-meteo"
    assert weather_body["result"]["spoken"] == "Surat: clear sky, 28°C."
    assert weather_calls == [("what's the weather in Surat?", 3)]

    calibrate = await client.post(
        "/v1/gateway/tools",
        json={"name": "calibrate", "arguments": {}, "request_id": "agent4-calibrate"},
    )
    assert calibrate.status_code == 200, calibrate.text
    calibrate_result = calibrate.json()["result"]
    assert calibrate_result["ok"] is True
    assert calibrate_result["spoken"]
    assert calibrate_result["hud"]
    assert calibrate_result["evidence"]["source"] == "local"

    calendar = await _install_local(client, "calendar", ["calendar:read"])
    start = (utcnow() + timedelta(hours=1)).isoformat()
    event = await client.post(
        f"/v1/live/channels/{calendar['live_channel_id']}/events",
        json=[
            {
                "event_type": "calendar.event.updated",
                "payload": {
                    "summary": "Agent 4 local review",
                    "start": start,
                    "end": (utcnow() + timedelta(hours=2)).isoformat(),
                    "location": "Local lab",
                    "source": "local-test-provider",
                },
            }
        ],
    )
    assert event.status_code == 201, event.text
    calendar_read = await client.post(
        "/v1/gateway/tools",
        json={
            "name": "calendar_read",
            "arguments": {"limit": 8},
            "request_id": "agent4-calendar",
        },
    )
    assert calendar_read.status_code == 200, calendar_read.text
    calendar_result = calendar_read.json()["result"]
    assert calendar_result["ok"] is True
    assert calendar_result["next_event"]["summary"] == "Agent 4 local review"
    assert calendar_result["evidence"]["source"] == "calendar_live_events"
    assert calendar_result["evidence"]["event_ids"]
    assert "Agent 4 local review" in calendar_result["spoken"]

    timer_request = resolve_live_action("start a timer for 1 minute")
    assert timer_request == ("start_timer", {"minutes": 1})
    timer = await client.post(
        "/v1/gateway/tools",
        json={
            "name": timer_request[0],
            "arguments": {**timer_request[1], "text": "Agent 4 acceptance"},
            "request_id": "agent4-timer",
        },
    )
    assert timer.status_code == 200, timer.text
    timer_result = timer.json()["result"]
    assert timer_result["ok"] is True
    assert timer_result["timer_id"] == timer_result["id"]
    assert timer_result["evidence"]["source"] == "owner_timer"
    assert timer_result["evidence"]["accepted"] is True
    assert timer_result["evidence"]["observed"] is True
    assert timer_result["spoken"].startswith("Timer set for ")

    logs = list(
        (
            await db_session.execute(
                select(AccessLog).where(AccessLog.action == "tool_call")
            )
        )
        .scalars()
        .all()
    )
    by_request = {row.request_id: row for row in logs}
    for request_id in ("agent4-weather", "agent4-calibrate", "agent4-calendar", "agent4-timer"):
        assert request_id in by_request
        assert by_request[request_id].details["policy_effect"] == "allow"
        assert by_request[request_id].details["status"] == "ok"

    timer_rows = (await db_session.execute(select(OwnerTimer))).scalars().all()
    assert len(timer_rows) == 1
    assert timer_rows[0].payload["text"] == "Agent 4 acceptance"


async def test_agent4_live_timer_continuation_speaks_from_local_result(
    db_session: AsyncSession,
) -> None:
    reset_live_registry()
    live = LiveSession(session_id="agent4-timer-live", device_id="agent4-mac", backchannel_enabled=False)
    live.run_live_tool = _grok_tool_runner(actor="voice", device_id=None, live=live)
    try:
        handled = await live._maybe_local_intent("start a timer for 1 minute", from_grok=False)
        assert handled is True
        events = _drain(live)
        assert any(isinstance(event, HudEvent) and event.kind == "progress" for event in events)
        evidence = [event for event in events if isinstance(event, HudEvent) and event.kind == "evidence"]
        assert evidence
        assert evidence[-1].card["meta"]["source"] == "owner_timer"
        replies = [event for event in events if isinstance(event, ReplyEvent)]
        assert any(reply.text.startswith("Timer set for ") for reply in replies)

        logs = list(
            (
                await db_session.execute(
                    select(AccessLog).where(AccessLog.resource_type == "tool")
                )
            )
            .scalars()
            .all()
        )
        assert any(
            row.resource_ids and "start_timer" in row.resource_ids and row.details["channel"] == "voice"
            for row in logs
        )
    finally:
        live.close()


async def test_agent4_safe_failures_are_degraded_and_never_local_success(
    client: AsyncClient,
) -> None:
    missing_provider = await client.post(
        "/v1/gateway/tools",
        json={
            "name": "calendar_read",
            "arguments": {"limit": 8},
            "request_id": "agent4-missing-provider",
        },
    )
    assert missing_provider.status_code == 200, missing_provider.text
    missing_result = missing_provider.json()["result"]
    assert missing_result["ok"] is False
    assert missing_result["error"] == "not_connected"
    assert missing_result["degraded"] is True
    assert "calendar" in missing_result["next_step"]

    await _install_local(client, "calendar", ["calendar:act"], slug="calendar-wrong-scope")
    missing_scope = await client.post(
        "/v1/gateway/tools",
        json={
            "name": "calendar_read",
            "arguments": {"limit": 8},
            "request_id": "agent4-missing-scope",
        },
    )
    assert missing_scope.status_code == 200, missing_scope.text
    scope_body = missing_scope.json()
    assert scope_body["ok"] is False
    assert scope_body["result"]["ok"] is False
    assert "missing scopes" in scope_body["result"]["error"]

    invalid = await client.post(
        "/v1/gateway/tools",
        json={
            "name": "start_timer",
            "arguments": {"minutes": -1},
            "request_id": "agent4-invalid-args",
        },
    )
    assert invalid.status_code == 200, invalid.text
    invalid_body = invalid.json()
    assert invalid_body["ok"] is False
    assert invalid_body["result"]["ok"] is False
    assert invalid_body["result"]["error"] == "invalid_request"
    assert "minutes" in invalid_body["result"]["reason"]
    assert "invalid" in (invalid_body["result"].get("spoken") or "").lower()


async def test_agent4_confirmation_parks_resumes_with_local_evidence_and_expires(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    await _unlock_training_wheels(db_session)
    await _install_local(
        client,
        "phone",
        ["phone:act"],
        config={"provider": "local", "simulate_opened": True},
    )

    reset_live_registry()
    live = LiveSession(session_id="agent4-confirm", device_id="agent4-mac", backchannel_enabled=False)
    register_live(live)
    try:
        runner = _grok_tool_runner(actor="voice", device_id=None, live=live)
        raw = await runner(
            "place_call",
            {"name": "Ned", "confirm": True},
            "agent4-call-confirm",
        )
        hold = json.loads(raw)
        assert hold["ok"] is False
        assert hold["error"] == "confirmation_required"
        assert hold["result"]["confirmation_required"] is True
        action_id = hold["result"]["action_id"]
        assert live._approval_hold is not None
        assert any(
            isinstance(event, HudEvent) and event.kind == "approval_hold"
            for event in _drain(live)
        )

        approved = await client.post(f"/v1/runtime/actions/{action_id}/approve", json={})
        assert approved.status_code == 200, approved.text
        approved_body = approved.json()
        assert approved_body["status"] == "executed"
        assert approved_body["result"]["ok"] is True
        assert approved_body["result"]["opened"] is True
        assert approved_body["result"]["evidence"]["source"] == "local"
        assert approved_body["result"]["evidence"]["observed"] is True

        resumed = _drain(live)
        assert any(
            isinstance(event, HudEvent)
            and event.kind == "evidence"
            and event.card["meta"]["source"] == "local"
            for event in resumed
        )
        assert any(isinstance(event, ReplyEvent) and "Ringing Ned" in event.text for event in resumed)
        assert live._approval_hold is None

        expired_raw = await runner(
            "place_call",
            {"name": "Ned", "confirm": True},
            "agent4-call-expired",
        )
        expired = json.loads(expired_raw)
        expired_id = expired["result"]["action_id"]
        action = await db_session.get(ApprovedAction, UUID(expired_id))
        assert action is not None
        payload = dict(action.payload)
        metadata = dict(payload["_pol"])
        metadata["expires_at"] = (utcnow() - timedelta(seconds=1)).isoformat()
        payload["_pol"] = metadata
        action.payload = payload
        await db_session.commit()

        expired_approval = await client.post(f"/v1/runtime/actions/{expired_id}/approve", json={})
        assert expired_approval.status_code == 409, expired_approval.text
        assert "confirmation expired" in expired_approval.json()["detail"]
        await db_session.refresh(action)
        assert action.status == "denied"
        assert action.denied_reason == "confirmation_expired"

        logs = list(
            (
                await db_session.execute(
                    select(AccessLog).where(AccessLog.action.in_(["tool_call", "action.decide"]))
                )
            )
            .scalars()
            .all()
        )
        assert any(
            row.action == "tool_call"
            and row.resource_ids
            and "place_call" in row.resource_ids
            and row.details["policy_effect"] == "allow"
            for row in logs
        )
        assert any(row.action == "action.decide" and row.details["decision"] == "approve" for row in logs)
    finally:
        unregister_live(live)
        live.close()


async def test_agent4_realtime_harness_deduplicates_call_id() -> None:
    fake = _FakeRealtime()
    calls: list[tuple[str, dict, str]] = []
    called = asyncio.Event()

    async def connect(_url: str, additional_headers=None):
        del additional_headers
        return fake

    async def on_tool(name: str, arguments: dict, call_id: str) -> str:
        calls.append((name, arguments, call_id))
        called.set()
        return json.dumps(
            {
                "ok": True,
                "name": name,
                "result": {"ok": True, "spoken": "done", "evidence": {"source": "local"}},
            }
        )

    bridge = GrokVoiceBridge(
        on_event=lambda _event: asyncio.sleep(0),
        on_tool=on_tool,
        connect=connect,
        api_key="test",
        provider="openai",
        approved_tool_specs=[
            {
                "type": "function",
                "name": "start_timer",
                "description": "Run start_timer.",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
            }
        ],
    )
    try:
        assert await bridge.start() is True
        await _acknowledge_session(bridge, fake)
        event = {
            "type": "response.function_call_arguments.done",
            "name": "start_timer",
            "call_id": "agent4-duplicate-call",
            "arguments": json.dumps({"value": "tea"}),
        }
        await fake.incoming.put(json.dumps(event))
        await fake.incoming.put(json.dumps(event))
        await asyncio.wait_for(called.wait(), timeout=1)
        await _wait_for(
            lambda: len(
                [
                    item
                    for item in fake.sent
                    if item.get("type") == "conversation.item.create"
                    and item.get("item", {}).get("type") == "function_call_output"
                ]
            )
            == 1
        )
        assert calls == [("start_timer", {"value": "tea"}, "agent4-duplicate-call")]
    finally:
        bridge.close()
