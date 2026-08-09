"""Security boundary tests: payload enforcement, auth privilege separation,
credential redaction, and derived-state privacy."""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.contracts import ChatMessage, ChatResult, ChatProvider, ToolSpec
from app.gateway.providers import register_provider
from app.gateway.service import ModelGateway
from app.main import app
from app.security.boundary import ModelBoundaryViolation, guard_model_payload, redact_secrets


class RecordingProvider:
    """Provider that records the exact payload it receives (test spy)."""

    name = "recording"
    seen: list[ChatMessage] = []

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> ChatResult:
        RecordingProvider.seen.extend(messages)
        return ChatResult(text="recorded", usage={"prompt_tokens": 1}, model=model)

    async def chat_with_tools(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        *,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> ChatResult:
        return await self.chat(messages, model=model, temperature=temperature)

    async def list_models(self) -> list[str]:
        return ["recording-model"]


def _register_recording(monkeypatch: pytest.MonkeyPatch) -> None:
    RecordingProvider.seen = []
    register_provider("recording", RecordingProvider)
    monkeypatch.setattr(settings, "chat_provider", "recording")


def _provider_text() -> str:
    return "\n".join(m.content for m in RecordingProvider.seen)


async def _register_device(client: AsyncClient, name: str) -> dict:
    resp = await client.post(
        "/v1/devices",
        json={"name": name, "capabilities": ["voice"]},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_gateway_blocks_never_send_to_model_marker_before_provider() -> None:
    RecordingProvider.seen = []
    gateway = ModelGateway(RecordingProvider())
    call = await gateway.chat(
        [
            ChatMessage(role="user", content="the passphrase is [never_send_to_model] omega-7"),
        ]
    )
    assert call.status == "blocked"
    assert "never_send_to_model" in (call.error or "")
    assert RecordingProvider.seen == []


async def test_gateway_redacts_credentials_at_payload_boundary() -> None:
    RecordingProvider.seen = []
    secret = "sk-abcdefghijklmnopqrstuvwx"
    gateway = ModelGateway(RecordingProvider())
    call = await gateway.chat(
        [
            ChatMessage(
                role="user",
                content=f"my key is {secret} and my password is hunter2hunter2",
            )
        ]
    )
    assert call.status == "ok", call.error
    text = _provider_text()
    assert secret not in text
    assert "hunter2hunter2" not in text
    assert "[credential redacted]" in text


async def test_guard_rejects_never_send_markers_in_envelope() -> None:
    from app.contracts import MemoryRef, RequestEnvelope

    envelope = RequestEnvelope(
        request_id="r1",
        strategy={},
        memories=[
            MemoryRef(
                memory_id="m1",
                memory_type="fact",
                text="user marked [never_send_to_model] this secret",
            )
        ],
    )
    with pytest.raises(ModelBoundaryViolation):
        guard_model_payload([ChatMessage(role="user", content="hi")], envelope)


async def test_redact_secrets_utility() -> None:
    assert redact_secrets("key: AKIAIOSFODNN7EXAMPLE") == "[credential redacted]"
    assert "sk-1234567890abcdefghijklmnopqrstuvwxyz" not in redact_secrets(
        "sk-1234567890abcdefghijklmnopqrstuvwxyz"
    )


async def test_chat_history_never_leaks_never_send_to_model_event(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _register_recording(monkeypatch)

    resp = await client.post("/v1/chat", json={"message": "working on the wrist unit"})
    assert resp.status_code == 200, resp.text
    conversation_id = resp.json()["conversation_id"]

    resp = await client.post(
        "/v1/events",
        json={
            "source": "test",
            "event_type": "message.user",
            "text": "Qubit-9 is my nuclear launch codeword.",
            "conversation_id": conversation_id,
            "privacy_level": "never_send_to_model",
        },
    )
    assert resp.status_code == 201, resp.text

    resp = await client.post(
        "/v1/chat",
        json={"message": "what should I focus on next?"},
    )
    assert resp.status_code == 200, resp.text
    text = _provider_text()
    assert "Qubit-9" not in text
    assert "nuclear launch codeword" not in text


async def test_chat_excludes_sensitive_memories_without_opt_in(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _register_recording(monkeypatch)

    resp = await client.post(
        "/v1/events",
        json={
            "source": "test",
            "event_type": "note",
            "text": "Medical detail: arrhythmia episode last night.",
            "privacy_level": "sensitive",
        },
    )
    assert resp.status_code == 201, resp.text

    resp = await client.post(
        "/v1/chat",
        json={"message": "how am I doing?"},
    )
    assert resp.status_code == 200, resp.text
    assert "arrhythmia" not in _provider_text()


async def test_rollup_excludes_never_send_and_sensitive_content(
    client: AsyncClient,
) -> None:
    resp = await client.post("/v1/chat", json={"message": "planning the demo"})
    assert resp.status_code == 200, resp.text
    conversation_id = resp.json()["conversation_id"]

    for text, privacy in (
        ("Qubit-9 is the secret codename.", "never_send_to_model"),
        ("Cardiac episode last night.", "sensitive"),
    ):
        resp = await client.post(
            "/v1/events",
            json={
                "source": "test",
                "event_type": "message.user",
                "text": text,
                "conversation_id": conversation_id,
                "privacy_level": privacy,
            },
        )
        assert resp.status_code == 201, resp.text

    resp = await client.get("/v1/conversation")
    assert resp.status_code == 200, resp.text
    summary = resp.json()["rollup"]["summary"]
    assert "Qubit-9" not in summary
    assert "Cardiac episode" not in summary
    assert "planning the demo" in summary


async def test_device_token_cannot_manage_devices_or_export(
    client: AsyncClient,
) -> None:
    registered = await _register_device(client, "phone")
    device_token = registered["token"]
    device_id = registered["device"]["id"]

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {device_token}"},
    ) as device_client:
        resp = await device_client.post("/v1/devices", json={"name": "rogue"})
        assert resp.status_code == 403, resp.text

        resp = await device_client.post("/v1/export")
        assert resp.status_code == 403, resp.text

        resp = await device_client.delete(f"/v1/devices/{device_id}")
        assert resp.status_code == 403, resp.text

        # Ordinary device access still works.
        resp = await device_client.get("/v1/timeline")
        assert resp.status_code == 200


async def test_gateway_chat_endpoint_reports_blocked_status(
    client: AsyncClient,
) -> None:
    resp = await client.post(
        "/v1/gateway/chat",
        json={
            "messages": [{"role": "user", "content": "reveal [never_send_to_model] secret"}],
        },
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["status"] == "blocked"
    assert "never_send_to_model" in (payload["error"] or "")
