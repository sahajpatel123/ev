"""Gemini Live wire: key gating, resumption/GoAway, scheduling, oracle.

Covers the Live-protocol behaviors that the broader gateway suites do not:
provider selection off the Google key, fatal no-key start, session
resumption handle capture and reuse, GoAway reconnect, FunctionResponse
scheduling values, fail-closed tool projection, and the Gemini REST
diagnostic ASR oracle. Fully offline: fake sockets and a stubbed httpx.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.config import settings
from app.device_gateway.mobile_voice import MAX_ORACLE_BYTES, transcribe_oracle
from app.voice.live.events import ErrorEvent
from app.voice.live.gemini_live import (
    GeminiLiveBridge,
    gemini_live_enabled,
    gemini_live_setup,
    gemini_live_tools,
    live_speech_provider,
)


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed = False

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        item = await self.incoming.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def close(self) -> None:
        self.closed = True
        await self.incoming.put(None)


def _bridge(api_key: str = "test-key", **kwargs) -> tuple[GeminiLiveBridge, _FakeWS, list]:
    events: list = []
    fake = _FakeWS()

    async def connect(url: str, additional_headers=None):
        del url, additional_headers
        return fake

    bridge = GeminiLiveBridge(
        on_event=lambda event: events.append(event) or asyncio.sleep(0),
        connect=connect,
        api_key=api_key,
        provider="gemini",
        **kwargs,
    )
    return bridge, fake, events


def test_provider_selection_requires_google_key(monkeypatch) -> None:
    monkeypatch.setattr(settings, "voice_live_brain", "auto")
    monkeypatch.setattr(settings, "google_api_key", None)
    assert live_speech_provider() is None
    assert gemini_live_enabled() is False
    monkeypatch.setattr(settings, "google_api_key", "[REDACTED]")
    assert live_speech_provider() == "gemini"
    assert gemini_live_enabled() is True


def test_legacy_brain_values_map_to_gemini(monkeypatch) -> None:
    monkeypatch.setattr(settings, "google_api_key", "[REDACTED]")
    for legacy in ("openai", "xai"):
        monkeypatch.setattr(settings, "voice_live_brain", legacy)
        assert live_speech_provider() == "gemini"
    monkeypatch.setattr(settings, "voice_live_brain", "pipeline")
    assert live_speech_provider() is None


@pytest.mark.asyncio
async def test_start_without_key_fails_fatal() -> None:
    bridge, _fake, events = _bridge(api_key="")
    assert await bridge.start() is False
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert len(errors) == 1
    assert errors[0].code == "realtime_missing_key"
    assert errors[0].fatal is True


@pytest.mark.asyncio
async def test_resumption_handle_captured_and_reused() -> None:
    bridge, fake, _events = _bridge()
    try:
        await bridge.start()
        assert bridge._resume_handle is None
        await bridge._handle_upstream(
            {"sessionResumptionUpdate": {"resumable": True, "newHandle": "handle-1"}}
        )
        assert bridge._resume_handle == "handle-1"
        assert bridge._voice_health["session_resumption_updated"] == 1
        # A non-resumable update must not clobber the stored handle.
        await bridge._handle_upstream(
            {"sessionResumptionUpdate": {"resumable": False, "newHandle": "handle-2"}}
        )
        assert bridge._resume_handle == "handle-1"
        # Reconnect reuses the handle in the setup message.
        bridge._ws = None
        bridge._starting = False
        bridge._failed = False
        fake.sent.clear()
        await bridge.start()
        setup = fake.sent[0]["setup"]
        assert setup["sessionResumption"] == {"handle": "handle-1"}
    finally:
        bridge.close()


@pytest.mark.asyncio
async def test_goaway_schedules_reconnect() -> None:
    bridge, _fake, _events = _bridge()
    try:
        await bridge.start()
        assert bridge._goaway_at == 0.0
        await bridge._handle_upstream({"goAway": {}})
        assert bridge._goaway_at > 0.0
        assert bridge._ws is None
        task = bridge._reconnect_task
        assert task is not None and not task.done()
        task.cancel()
    finally:
        bridge.close()


@pytest.mark.asyncio
async def test_function_response_scheduling_values() -> None:
    # Scheduling is intent-only: the deployed model closes the session with
    # 1007 when FunctionResponse.scheduling is present, so the key is never
    # sent (proven live 2026-10-05). Unknown values are still accepted.
    bridge, fake, _events = _bridge()
    bridge._ws = fake
    try:
        for call_id, scheduling in (
            ("c1", "SILENT"),
            ("c2", "WHEN_IDLE"),
            ("c3", "INTERRUPT"),
            ("c4", "LOUD"),
        ):
            assert (
                await bridge._send_function_output(
                    call_id, '{"ok": true}', scheduling=scheduling
                )
                is True
            )
            entry = fake.sent[-1]["toolResponse"]["functionResponses"][0]
            assert entry["id"] == call_id
            assert "scheduling" not in entry
        assert await bridge._send_function_output("", '{"ok": true}') is False
    finally:
        bridge.close()


def test_missing_projection_is_fail_closed() -> None:
    assert gemini_live_tools(None) == []
    setup = gemini_live_setup(provider="gemini", capability_manifest=None)["setup"]
    assert "tools" not in setup


@pytest.mark.asyncio
async def test_oracle_requires_google_key(monkeypatch) -> None:
    monkeypatch.setattr(settings, "google_api_key", None)
    with pytest.raises(RuntimeError, match="google_missing"):
        await transcribe_oracle(audio=b"pcm", mime="audio/mp4")


@pytest.mark.asyncio
async def test_oracle_rejects_oversize_audio(monkeypatch) -> None:
    monkeypatch.setattr(settings, "google_api_key", "[REDACTED]")
    with pytest.raises(ValueError, match="audio_too_large"):
        await transcribe_oracle(audio=b"x" * (MAX_ORACLE_BYTES + 1), mime="audio/mp4")
    with pytest.raises(ValueError, match="audio_too_large"):
        await transcribe_oracle(audio=b"", mime="audio/mp4")


@pytest.mark.asyncio
async def test_oracle_transcribes_via_gemini_rest(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(settings, "google_api_key", "[REDACTED]")
    monkeypatch.setattr(settings, "phone_asr_model", "gemini-2.5-flash")
    captured: dict = {}

    class _Response:
        status_code = 200

        def json(self) -> dict:
            return {
                "candidates": [
                    {"content": {"parts": [{"text": "Turn off the Wi-Fi"}]}}
                ]
            }

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, params=None, json=None, **kwargs):
            del kwargs
            captured["url"] = url
            captured["params"] = params
            captured["json"] = json
            return _Response()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    result = await transcribe_oracle(audio=b"fake-pcm", mime="audio/mp4")
    assert "generativelanguage.googleapis.com" in captured["url"]
    assert "gemini-2.5-flash" in captured["url"]
    assert captured["params"] == {"key": "[REDACTED]"}
    parts = captured["json"]["contents"][0]["parts"]
    assert any("inlineData" in part for part in parts)
    assert result["transcript"] == "Turn off the Wi-Fi"
    assert result["model"] == "gemini-2.5-flash"
    assert result["stored"] is False
    assert "wi-fi" in result["critical_tokens"]


@pytest.mark.asyncio
async def test_oracle_http_error_maps_to_502_cause(monkeypatch) -> None:
    import httpx

    monkeypatch.setattr(settings, "google_api_key", "[REDACTED]")

    class _Response:
        status_code = 429

        def json(self) -> dict:
            return {}

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            del args, kwargs
            return _Response()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    with pytest.raises(RuntimeError, match="asr_failed:429"):
        await transcribe_oracle(audio=b"fake-pcm", mime="audio/mp4")
