"""Versioned Device Protocol constants. Reuse live voice binary PCM on /v1/voice/live."""

from __future__ import annotations

from . import PROTOCOL_VERSION

SUPPORTED_PROTOCOL_RANGE = (1, 1)

MESSAGES = (
    "hello",
    "authenticate",
    "capability_update",
    "presence",
    "conversation_claim",
    "conversation_release",
    "user_text",
    "audio_start",
    "audio_chunk",
    "audio_end",
    "camera_result",
    "tool_request",
    "tool_result",
    "assistant_text",
    "assistant_audio",
    "error",
    "heartbeat",
    "conversation_moved",
    "look_frame",
)

# PCM WebSocket fallback contract. WebRTC uses the browser media track instead.
AUDIO_CONTRACT = {
    "codec": "pcm16le",
    "sample_rate": 16000,
    "channels": 1,
    "frame_duration_ms": 20,
    "fallback_only": True,
}


def protocol_compatible(client_version: str | int | None) -> bool:
    try:
        version = int(str(client_version or PROTOCOL_VERSION).split(".")[0])
    except ValueError:
        return False
    low, high = SUPPORTED_PROTOCOL_RANGE
    return low <= version <= high
