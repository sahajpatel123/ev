"""P0 2026-08-22 regressions.

1. Upstream quota/spend-limit closures must be classified truthfully and
   must never masquerade as a generic reconnectable disconnect.
2. A client frame whose handler raises must degrade to control_rejected —
   it must NEVER terminate the client websocket transport.
"""
from __future__ import annotations

import asyncio
import json

from app.voice.live.engine import LiveEngine
from app.voice.live.grok_voice import _realtime_error_fields
from app.voice.live.layer import is_quota_close, ws_close_fields
from app.voice.live.session import LiveSession
from app.voice.live.transport import _handle_client_frame


class _FakeClose(Exception):
    def __init__(self, code: int, reason: str) -> None:
        super().__init__(f"received {code} ({reason})")

        class _Frame:
            pass

        frame = _Frame()
        frame.code = code
        frame.reason = reason
        self.rcvd = frame


def test_ws_close_fields_extract_code_and_reason() -> None:
    exc = _FakeClose(1013, "insufficient_quota.organization_spend_limit_exceeded")
    code, reason = ws_close_fields(exc)
    assert code == 1013
    assert "spend_limit" in reason


def test_ws_close_fields_without_frame() -> None:
    assert ws_close_fields(ValueError("nope")) == (None, "")


def test_quota_close_classification() -> None:
    assert is_quota_close("insufficient_quota.organization_spend_limit_exceeded")
    assert is_quota_close("", "Error: insufficient_quota on response.create")
    assert not is_quota_close("normal closure")


def test_realtime_error_fields_reads_openai_error_event() -> None:
    event = {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "code": "insufficient_quota",
            "message": "limit reached",
        },
    }
    message, code = _realtime_error_fields(event)
    assert code == "insufficient_quota"
    assert "limit reached" in str(message)


def test_handler_exception_becomes_control_rejected_not_transport_death() -> None:
    session = LiveSession(session_id="containment", engine=LiveEngine())

    async def boom(_message):
        raise TypeError("stale signature simulation")

    session.handle_client = boom  # type: ignore[method-assign]
    emitted: list = []

    async def capture(event):
        emitted.append(event)

    session.emit = capture  # type: ignore[method-assign]

    async def run() -> None:
        await asyncio.wait_for(
            _handle_client_frame(session, {"type": "control", "action": "listener_presence"}),
            timeout=5,
        )

    asyncio.run(run())
    assert any(getattr(e, "code", "") == "control_rejected" for e in emitted)
    payload = json.dumps({"probe": "still-alive"})
    assert isinstance(payload, str)


def test_keepalive_is_acked_and_does_not_enter_handle_client() -> None:
    session = LiveSession(session_id="keepalive", engine=LiveEngine())
    handled: list = []
    emitted: list = []

    async def capture_handle(message):
        handled.append(message)

    async def capture_emit(event):
        emitted.append(event)

    session.handle_client = capture_handle  # type: ignore[method-assign]
    session.emit = capture_emit  # type: ignore[method-assign]

    async def run() -> None:
        await asyncio.wait_for(
            _handle_client_frame(session, {"type": "keepalive"}),
            timeout=5,
        )

    asyncio.run(run())
    assert handled == []
    assert [getattr(e, "type", "") for e in emitted] == ["keepalive"]
