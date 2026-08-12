"""API tests for the gateway envelope, tool validation, and model-call audit."""

from __future__ import annotations

from uuid import uuid4

from httpx import AsyncClient

from app.config import settings
from app.contracts import ChatResult, ToolCall
from app.gateway.providers import register_provider
from app.gateway.reliability import CIRCUIT_BREAKERS
from app.models import ModelCallLog
from app.services.model_call import model_call_stats


class ToolMockProvider:
    """Test provider that returns a deterministic tool call."""

    name = "toolmock"

    async def chat(self, messages, *, model=None, temperature=0.7) -> ChatResult:
        return ChatResult(text="EV: ok", usage={"prompt_tokens": 1, "completion_tokens": 1}, model=model)

    async def chat_with_tools(self, messages, tools, *, model=None, temperature=0.7) -> ChatResult:
        return ChatResult(
            text="",
            tool_calls=[ToolCall(id="tc-1", name="calculate", arguments={"expression": "2+2"})],
            usage={"prompt_tokens": 2, "completion_tokens": 1},
            model=model,
        )

    async def list_models(self) -> list[str]:
        return ["toolmock-model"]


async def test_chat_logs_auditable_model_call_with_envelope(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/chat",
        json={"message": "Remember I prefer espresso over drip coffee."},
    )
    assert resp.status_code == 200, resp.text
    request_id = resp.json()["request_id"]
    assert request_id

    resp = await client.get("/v1/gateway/calls", params={"request_id": request_id})
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["request_id"] == request_id
    assert row["provider"] == "mock"
    assert row["model"] == "mock-model"
    assert row["status"] == "ok"
    assert row["latency_ms"] >= 0
    assert row["envelope"]["strategy"]["intent"]
    assert row["envelope"]["conversation_id"]
    assert row["envelope"]["context_tokens"] > 0


async def test_gateway_chat_forwards_tools_validates_and_audits(
    client: AsyncClient,
) -> None:
    register_provider("toolmock", ToolMockProvider)
    original = settings.chat_provider
    try:
        settings.chat_provider = "toolmock"
        resp = await client.post(
            "/v1/gateway/chat",
            json={
                "messages": [{"role": "user", "content": "What is 2+2?"}],
                "request_id": "api-req-1",
                "strategy": {"mode": "technical", "intent": "question"},
                "memories": [
                    {
                        "memory_id": "mem-1",
                        "memory_type": "fact",
                        "text": "espresso preference",
                        "score": 0.8,
                    }
                ],
                "context": {
                    "context_tokens": 42,
                    "context_depth": "standard",
                    "envelope_hash": "hash-abc-123",
                },
                "tools": [
                    {
                        "name": "calculate",
                        "description": "Arithmetic.",
                        "parameters": {
                            "type": "object",
                            "properties": {"expression": {"type": "string"}},
                            "required": ["expression"],
                        },
                    }
                ],
                "model": "toolmock-model",
            },
        )
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["request_id"] == "api-req-1"
        assert payload["provider"] == "toolmock"
        assert payload["model"] == "toolmock-model"
        assert payload["status"] == "ok"
        assert payload["tool_calls"] == [{"id": "tc-1", "name": "calculate", "arguments": {"expression": "2+2"}}]
        assert payload["tool_validation"][0]["status"] == "ok"
        assert payload["envelope"]["memories"][0]["memory_id"] == "mem-1"
        assert payload["envelope"]["strategy"]["mode"] == "technical"
        assert payload["envelope"]["metadata"]["envelope_hash"] == "hash-abc-123"
        assert payload["latency_ms"] >= 0

        resp = await client.get("/v1/gateway/calls", params={"request_id": "api-req-1"})
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["tool_calls"][0]["status"] == "ok"
        assert rows[0]["envelope"]["strategy"]["mode"] == "technical"
        assert rows[0]["envelope"]["memories"][0]["memory_id"] == "mem-1"
        assert rows[0]["envelope_hash"] == "hash-abc-123"
    finally:
        settings.chat_provider = original


async def test_model_call_stats_aggregates_routing_evidence(
    client: AsyncClient, db_session
) -> None:
    """Latency/error/token evidence per provider and model for eval-gated routing."""

    for i, (provider, model, status, latency) in enumerate(
        [
            ("mock", "mock-model", "ok", 10.0),
            ("mock", "mock-model", "error", 200.0),
            ("deepseek", "deepseek-v4-flash-0731", "ok", 50.0),
        ]
    ):
        db_session.add(
            ModelCallLog(
                request_id=f"stats-req-{i}",
                actor="tester",
                provider=provider,
                model=model,
                status=status,
                latency_ms=latency,
                prompt_tokens=10,
                completion_tokens=5,
                envelope={},
            )
        )
    await db_session.flush()
    await db_session.commit()

    stats = await model_call_stats(db_session, window_hours=24)
    assert stats["totals"]["calls"] == 3
    assert stats["totals"]["errors"] == 1
    assert stats["totals"]["avg_latency_ms"] == round((10 + 200 + 50) / 3, 1)
    assert stats["totals"]["p95_latency_ms"] == 200.0
    assert stats["totals"]["prompt_tokens"] == 30

    by_provider = {b["provider"]: b for b in stats["by_provider_model"]}
    assert by_provider["mock"]["calls"] == 2
    assert by_provider["mock"]["errors"] == 1
    assert by_provider["deepseek"]["model"] == "deepseek-v4-flash-0731"
    assert by_provider["deepseek"]["calls"] == 1

    resp = await client.get("/v1/gateway/stats", params={"window_hours": 24})
    assert resp.status_code == 200, resp.text
    endpoint_stats = resp.json()
    assert endpoint_stats["totals"]["calls"] == 3
    assert endpoint_stats["totals"]["errors"] == 1
    assert endpoint_stats["window_hours"] == 24
    endpoint_providers = {b["provider"] for b in endpoint_stats["by_provider_model"]}
    assert endpoint_providers == {"mock", "deepseek"}


async def test_memory_only_endpoints_work_with_provider_unreachable(
    client: AsyncClient, monkeypatch
) -> None:
    """EV stays a usable offline second brain when the reasoning API is down."""

    monkeypatch.setattr(settings, "chat_provider", "deepseek")
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    monkeypatch.setattr(settings, "deepseek_base_url", "http://127.0.0.1:9")
    monkeypatch.setattr(settings, "model_max_retries", 0)
    CIRCUIT_BREAKERS.reset("deepseek")

    resp = await client.post(
        "/v1/events",
        json={
            "event_type": "note",
            "text": "offline memory still works",
            "source": "test",
        },
    )
    assert resp.status_code == 201, resp.text

    timeline = await client.get("/v1/timeline")
    assert timeline.status_code == 200, timeline.text
    assert any(
        event["content"].get("text") == "offline memory still works"
        for event in timeline.json()["events"]
    )

    memories = await client.get("/v1/memories")
    assert memories.status_code == 200, memories.text

    recall = await client.get("/v1/recall/week", params={"week_start": "2026-08-12"})
    assert recall.status_code == 200, recall.text

    audit = await client.get("/v1/gateway/calls")
    assert audit.status_code == 200, audit.text

    missing = await client.get(f"/v1/audit/{uuid4()}")
    assert missing.status_code == 404  # route functional, memory absent

    chat = await client.post("/v1/chat", json={"message": "reason please"})
    assert chat.status_code == 503, chat.text
    assert "unavailable" in chat.json()["detail"]


async def test_chat_refuses_when_monthly_cost_cap_exceeded(
    client: AsyncClient, db_session, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "chat_provider", "deepseek")
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    monkeypatch.setattr(settings, "deepseek_base_url", "http://127.0.0.1:9")
    monkeypatch.setattr(settings, "model_max_retries", 0)
    monkeypatch.setattr(settings, "monthly_cost_cap_usd", 40.0)
    monkeypatch.setattr(settings, "cost_cap_enabled", True)
    db_session.add(
        ModelCallLog(
            request_id="cost-cap-usage",
            actor="tester",
            provider="deepseek",
            model="deepseek-test",
            status="ok",
            latency_ms=10,
            prompt_tokens=200_000_000,  # ≈ $54 at $0.27/1M input
            completion_tokens=0,
            envelope={},
        )
    )
    await db_session.commit()

    resp = await client.post("/v1/chat", json={"message": "expensive thought"})
    assert resp.status_code == 503, resp.text
    assert "cost cap" in resp.json()["detail"]
