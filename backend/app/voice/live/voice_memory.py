"""Bounded owner-utterance durability for live Realtime.

This is voice ingestion, not a second memory architecture. Each committed
owner speech turn must end in a durable Event or an explicit failure.
"""

from __future__ import annotations

import base64
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.voice.contracts import Transcriber

logger = logging.getLogger("ev.voice.live.grok")

# Wait for provider transcription before falling back. 2.5s is not enough:
# Realtime input transcription is async and often arrives after response.done.
DRAIN_TIMEOUT_S = 8.0
PERSIST_FLUSH_TIMEOUT_S = 4.0
MAX_UTTERANCE_BYTES = 16_000 * 2 * 15  # 15s of 16 kHz PCM16
PREFIX_BYTES = int(16_000 * 2 * 0.2)  # ~200ms of pre-roll

FallbackTranscriber = Callable[[bytes, int], Awaitable[str]]


@dataclass
class UserAudioTurn:
    local_turn_id: str
    provider_item_id: str | None = None
    provider_session_id: str | None = None
    audio_committed: bool = False
    transcription_expected: bool = False
    transcription_received: bool = False
    transcript_text: str = ""
    transcript_source: str | None = None
    persistence_started: bool = False
    persistence_committed: bool = False
    persist_failed: bool = False
    committed_at: float = 0.0
    pcm: bytearray = field(default_factory=bytearray)
    status: str = "open"

    def append_pcm(self, pcm: bytes) -> None:
        if self.transcription_received or not pcm:
            return
        self.pcm.extend(pcm)
        overflow = len(self.pcm) - MAX_UTTERANCE_BYTES
        if overflow > 0:
            del self.pcm[:overflow]

    def release_pcm(self) -> None:
        self.pcm = bytearray()

    def awaiting_transcript(self) -> bool:
        return (
            self.audio_committed
            and self.transcription_expected
            and not self.transcription_received
            and not self.persist_failed
        )

    def memory_safe(self) -> bool:
        return self.persistence_committed and bool(self.transcript_text.strip())


_HEALTH: dict[str, Any] = {
    "pending_voice_turns": 0,
    "last_voice_transcript_status": None,
    "last_voice_event_commit_at": None,
    "last_turn_memory_safe": False,
    "last_transcription_latency_ms": None,
    "durable_voice_memory_ready": False,
    "provider_session_id": None,
    "realtime_input_transcription": {
        "requested": False,
        "provider_confirmed": False,
        "model": None,
    },
}


def health_snapshot() -> dict[str, Any]:
    """Runtime voice-memory health. Never includes transcript text."""

    tx = _HEALTH["realtime_input_transcription"]
    return {
        "pending_voice_turns": int(_HEALTH["pending_voice_turns"]),
        "last_voice_transcript_status": _HEALTH["last_voice_transcript_status"],
        "last_voice_event_commit_at": _HEALTH["last_voice_event_commit_at"],
        "last_turn_memory_safe": bool(_HEALTH["last_turn_memory_safe"]),
        "last_transcription_latency_ms": _HEALTH["last_transcription_latency_ms"],
        "durable_voice_memory_ready": bool(_HEALTH["durable_voice_memory_ready"]),
        "provider_session_id": _HEALTH["provider_session_id"],
        "realtime_input_transcription": {
            "requested": bool(tx.get("requested")),
            "provider_confirmed": bool(tx.get("provider_confirmed")),
            "model": tx.get("model"),
        },
    }


def note_status(status: str, **extra: Any) -> None:
    _HEALTH["last_voice_transcript_status"] = status
    if status == "event_committed":
        _HEALTH["last_voice_event_commit_at"] = datetime.now(UTC).isoformat()
        _HEALTH["last_turn_memory_safe"] = True
    elif status in {"persist_failed", "transcription_timeout"}:
        _HEALTH["last_turn_memory_safe"] = False
    payload = " ".join(f"{key}={value}" for key, value in extra.items() if value is not None)
    if payload:
        logger.info("realtime_trace event=%s %s", status, payload)
    else:
        logger.info("realtime_trace event=%s", status)


def note_pending(count: int) -> None:
    _HEALTH["pending_voice_turns"] = max(0, int(count))


def note_transcription_config(
    *,
    requested: bool,
    provider_confirmed: bool,
    model: str | None,
    provider_session_id: str | None = None,
) -> None:
    _HEALTH["realtime_input_transcription"] = {
        "requested": bool(requested),
        "provider_confirmed": bool(provider_confirmed),
        "model": model,
    }
    _HEALTH["durable_voice_memory_ready"] = bool(requested and provider_confirmed)
    if provider_session_id:
        _HEALTH["provider_session_id"] = provider_session_id


def note_latency_ms(value: float | None) -> None:
    if value is None:
        return
    _HEALTH["last_transcription_latency_ms"] = int(max(0, value))


def note_event_committed() -> None:
    note_status("voice_memory.event_committed")


def note_persist_failed(*, reason: str) -> None:
    note_status("voice_memory.persist_failed", reason=reason)


async def transcribe_utterance_pcm(
    pcm: bytes,
    *,
    sample_rate: int = 16000,
    transcriber: FallbackTranscriber | None = None,
) -> str:
    """Transcribe one bounded owner utterance. Never logs the text."""

    if not pcm or len(pcm) < 320:
        return ""
    if transcriber is not None:
        spoken = await transcriber(pcm, sample_rate)
        return (spoken or "").strip()
    try:
        from app.audio.capture import pcm_to_wav_bytes
        from app.config import settings
        from app.voice.asr import OpenAICompatTranscriber, get_transcriber
    except Exception:  # noqa: BLE001 - fallback must never take down live
        logger.info("realtime_trace event=voice_memory.fallback_asr_unavailable")
        return ""
    wav = pcm_to_wav_bytes(pcm, sample_rate)
    audio_b64 = base64.b64encode(wav).decode("ascii")
    if (settings.openai_api_key or "").strip():
        engine: Transcriber = OpenAICompatTranscriber(
            base_url="https://api.openai.com/v1",
            api_key=settings.openai_api_key,
            model="whisper-1",
        )
        try:
            result = await engine.transcribe(audio_b64=audio_b64)
            if result is not None and not getattr(result, "degraded", False):
                return (result.text or "").strip()
        except Exception:  # noqa: BLE001
            logger.info("realtime_trace event=voice_memory.fallback_asr_openai_failed")
    provider = (settings.voice_asr_provider or "").strip()
    if provider and provider != "echo":
        try:
            engine = get_transcriber()
            result = await engine.transcribe(audio_b64=audio_b64)
            if result is not None and not getattr(result, "degraded", False):
                return (result.text or "").strip()
        except Exception:  # noqa: BLE001
            logger.info("realtime_trace event=voice_memory.fallback_asr_local_failed")
    return ""


def monotonic_ms_since(started_at: float) -> int | None:
    if not started_at:
        return None
    return int(max(0.0, (time.monotonic() - started_at) * 1000))
