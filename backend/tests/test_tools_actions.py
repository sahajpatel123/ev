"""Tests for the Tools & Actions layer: registry, dispatch, permissions, logging."""

from __future__ import annotations

from sqlalchemy import select

from app.contracts import ToolCall, ToolSpec
from app.gateway.validation import validate_output, validate_tool_calls
from app.models import AccessLog


async def test_registry_declares_execution_boundaries(client) -> None:
    resp = await client.get("/v1/tools")
    assert resp.status_code == 200, resp.text
    specs = resp.json()
    assert specs
    names = {s["name"] for s in specs}
    assert "calculate" in names
    assert "search_decisions" in names
    decisions_spec = next(s for s in specs if s["name"] == "search_decisions")
    assert decisions_spec["output"]["required"] == ["count", "results"]
    write_tools = [s for s in specs if s["read_only"] is False]
    assert {s["name"] for s in write_tools} >= {
        "send_message",
        "place_call",
        "open_url",
        "set_reminder",
    }
    for spec in specs:
        assert spec["parameters"]["additionalProperties"] is False
        assert isinstance(spec["output"], dict)
        assert spec["read_only"] in (True, False)
        assert spec["undoable"] in (True, False)
        assert spec["permission"]
    for spec in write_tools:
        assert spec["permission"]
    health = next(s for s in specs if s["name"] == "get_health_trends")
    assert health["sensitive"] is True
    assert health["permission"] == "health:read"


async def test_dispatcher_rejects_invalid_arguments(client) -> None:
    resp = await client.post(
        "/v1/gateway/tools",
        json={"name": "calculate", "arguments": {"expression": "1 + 1", "extra": True}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert "unknown argument 'extra'" in body["error"]

    resp = await client.post(
        "/v1/gateway/tools",
        json={"name": "calculate", "arguments": {}},
    )
    assert resp.json()["ok"] is False
    assert "missing required argument 'expression'" in resp.json()["error"]

    resp = await client.post(
        "/v1/gateway/tools",
        json={"name": "search_memory", "arguments": {"query": "sqlite", "k": 0}},
    )
    assert resp.json()["ok"] is False
    assert "must be >= 1" in resp.json()["error"]

    resp = await client.post(
        "/v1/gateway/tools",
        json={
            "name": "get_health_trends",
            "arguments": {"metric": "bogus"},
            "allow_sensitive": True,
        },
    )
    assert resp.json()["ok"] is False
    assert "must be one of" in resp.json()["error"]


async def test_sensitive_tool_requires_explicit_permission(client) -> None:
    resp = await client.post(
        "/v1/gateway/tools",
        json={"name": "get_health_trends", "arguments": {"metric": "sleep_hours"}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert "requires explicit permission" in body["error"]
    assert body["request_id"] is None

    resp = await client.post(
        "/v1/gateway/tools",
        json={
            "name": "get_health_trends",
            "arguments": {"metric": "sleep_hours", "window_days": 7},
            "allow_sensitive": True,
            "request_id": "req-tool-1",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["result"]["metric"] == "sleep_hours"
    assert body["request_id"] == "req-tool-1"
    assert body["actor"] == "master"


async def test_every_invocation_is_logged(client, db_session) -> None:
    await client.post(
        "/v1/gateway/tools",
        json={"name": "calculate", "arguments": {"expression": "2 + 3"}},
    )
    await client.post(
        "/v1/gateway/tools",
        json={"name": "get_health_trends", "arguments": {"metric": "sleep_hours"}},
    )

    rows = (
        await db_session.execute(
            select(AccessLog)
            .where(AccessLog.action == "tool_call")
            .order_by(AccessLog.occurred_at.desc())
        )
    ).scalars().all()
    by_tool = {row.resource_ids[0]: row for row in rows if row.resource_ids}
    assert set(by_tool) == {"calculate", "get_health_trends"}
    assert by_tool["calculate"].details["status"] == "ok"
    assert by_tool["get_health_trends"].details["status"] == "denied"
    assert by_tool["get_health_trends"].details["permission"] == "health:read"
    assert by_tool["get_health_trends"].details["sensitive"] is True
    assert "requires explicit permission" in by_tool["get_health_trends"].details["error"]


async def test_safe_calculate_hardening(client) -> None:
    unsafe = {
        "1 / 0": "Division by zero",
        "2 ** 200": "Exponent too large",
        "1e308 * 10": "Result is not finite",
        "__import__('os')": "Unsupported expression",
    }
    for expression, expected in unsafe.items():
        resp = await client.post(
            "/v1/gateway/tools",
            json={"name": "calculate", "arguments": {"expression": expression}},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is False, expression
        assert expected in body["error"], (expression, body["error"])

    long_expression = " + ".join(["1"] * 201)
    resp = await client.post(
        "/v1/gateway/tools",
        json={"name": "calculate", "arguments": {"expression": long_expression}},
    )
    assert resp.json()["ok"] is False
    assert "at most 200 characters" in resp.json()["error"]


async def test_tool_selection_routes_extended_intents(client) -> None:
    cases = {
        "what's 14% of 3,500?": "calculate",
        "compute 12 * 12": "calculate",
        "who is Maya?": "get_person",
        "anything on my calendar tomorrow?": "calendar_read",
        # EV VOICE CONTROL PLAN: past-tense history questions route to the
        # dedicated chunked past-history tool, not the generic search.
        "what did I decide about SQLite?": "recall_history",
    }
    for message, expected in cases.items():
        resp = await client.post("/v1/gateway/select-tool", json={"message": message})
        assert resp.status_code == 200, resp.text
        assert resp.json()["selected"] == expected, message


async def test_search_decisions_tool_returns_decision_memory(client) -> None:
    resp = await client.post(
        "/v1/events",
        json={
            "source": "test",
            "event_type": "note",
            "text": "I decided to use SQLite for local testing.",
        },
    )
    assert resp.status_code == 201, resp.text

    resp = await client.post(
        "/v1/gateway/tools",
        json={"name": "search_decisions", "arguments": {"query": "SQLite", "k": 5}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["result"]["count"] >= 1
    assert all(r["memory_type"] == "decision" for r in body["result"]["results"])
    assert any("SQLite" in r["text"] for r in body["result"]["results"])


def test_gateway_prevalidates_model_tool_calls() -> None:
    from dataclasses import fields as dc_fields

    from app.ev.tools import get_spec

    known = {item.name for item in dc_fields(ToolSpec)}

    def as_contract(name: str) -> ToolSpec:
        spec = get_spec(name)
        assert spec is not None
        return ToolSpec(**{key: value for key, value in spec.items() if key in known})

    specs = [as_contract("calculate")]
    good = validate_tool_calls(
        [ToolCall(id="1", name="calculate", arguments={"expression": "1+1"})],
        specs,
    )
    assert good[0].status == "ok"

    bad_type = validate_tool_calls(
        [ToolCall(id="2", name="calculate", arguments={"expression": 42})],
        specs,
    )
    assert bad_type[0].status == "rejected"
    assert "must be string" in bad_type[0].issues[0]

    unknown = validate_tool_calls(
        [ToolCall(id="3", name="search_web", arguments={})],
        specs,
    )
    assert unknown[0].status == "rejected"
    assert "unknown tool" in unknown[0].issues[0]

    health_spec = as_contract("get_health_trends")
    denied = validate_tool_calls(
        [ToolCall(id="4", name="get_health_trends", arguments={"metric": "sleep_hours"})],
        [health_spec],
        sensitive_allowed=False,
    )
    assert denied[0].status == "rejected"
    assert "requires explicit permission" in denied[0].issues[0]
    allowed = validate_tool_calls(
        [ToolCall(id="5", name="get_health_trends", arguments={"metric": "sleep_hours"})],
        [health_spec],
        sensitive_allowed=True,
    )
    assert allowed[0].status in {"ok", "rectified"}
    assert allowed[0].rectified_arguments["window_days"] == 14


def test_output_shape_validation() -> None:
    assert validate_output({"count": 1, "results": []}, {"type": "object", "required": ["count", "results"]}) == []
    assert validate_output({"count": 1}, {"type": "object", "required": ["count", "results"]}) == [
        "output missing required property 'results'"
    ]
    assert validate_output([], {"type": "array"}) == []
    assert validate_output({}, {"type": "array"}) == ["output must be array"]
