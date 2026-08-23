"""Client-confirmed near-end barge-in contract.

The Mac detector owns "the human started talking while Evie is speaking."
This module is the shared backend contract for that event: how we cancel,
how we ignore stale assistant output, and how we persist what the human
actually heard versus what the provider generated.

Provider VAD (`input_audio_buffer.speech_started`) is still treated as
echo-unsafe during playback. `interrupt_response` stays false. This path
is only taken after the client latches a confirmed barge-in.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

REASON_USER_BARGE_IN = "user_barge_in"
REASON_CLIENT_BARGE_IN = "client_barge_in"
REASON_CLIENT_CANCEL = "client_cancel"
REASON_TEXT_INPUT = "text_input"

# English TTS is roughly 12–16 characters per second. Used only when the
# provider did not give us a generated-audio duration.
_CHARS_PER_SECOND = 14.0


@dataclass(frozen=True)
class InterruptRequest:
    """One client-confirmed interruption of an in-flight assistant turn."""

    reason: str = REASON_USER_BARGE_IN
    audio_played_ms: int | None = None
    confidence: float | None = None
    preroll_ms: int | None = None
    provider_response_id: str | None = None


def parse_interrupt_request(message: dict | None) -> InterruptRequest:
    payload = message if isinstance(message, dict) else {}
    played = _optional_int(payload.get("audio_played_ms"))
    preroll = _optional_int(payload.get("preroll_ms"))
    confidence = _optional_float(payload.get("confidence"))
    reason = str(payload.get("reason") or payload.get("interruption_reason") or "").strip()
    if not reason:
        action = str(payload.get("action") or "").strip().lower()
        if action == "cancel":
            reason = REASON_CLIENT_CANCEL
        else:
            reason = REASON_CLIENT_BARGE_IN
    return InterruptRequest(
        reason=reason,
        audio_played_ms=played,
        confidence=confidence,
        preroll_ms=preroll,
        provider_response_id=_optional_str(payload.get("provider_response_id")),
    )


def generated_duration_ms(*, audio_bytes: int, sample_rate: int = 16000, text: str = "") -> int:
    """Best estimate of how long the generated assistant audio was."""

    if audio_bytes > 0 and sample_rate > 0:
        return max(0, int(audio_bytes / 2 / float(sample_rate) * 1000.0))
    spoken = (text or "").strip()
    if not spoken:
        return 0
    return max(0, int(len(spoken) / _CHARS_PER_SECOND * 1000.0))


def delivered_assistant_text(
    generated_text: str,
    *,
    audio_played_ms: int | None,
    generated_duration_ms: int | None,
) -> str:
    """Keep the heard prefix. Never treat unheard tail as spoken.

    Timing is approximate (no word-level alignment). When we cannot estimate
    a prefix, return empty so durable history does not invent a full reply.
    """

    spoken = (generated_text or "").strip()
    if not spoken:
        return ""
    played = audio_played_ms if audio_played_ms is not None else None
    generated = generated_duration_ms if generated_duration_ms is not None else None
    if played is None or played <= 0:
        return ""
    if generated is None or generated <= 0:
        generated = max(played, int(len(spoken) / _CHARS_PER_SECOND * 1000.0))
    if generated <= 0:
        return ""
    ratio = min(1.0, max(0.0, float(played) / float(generated)))
    if ratio >= 0.97:
        return spoken
    words = spoken.split()
    if not words:
        return ""
    keep = max(1, int(math.ceil(len(words) * ratio)))
    keep = min(keep, len(words))
    # Prefer a complete first clause when the user heard a meaningful prefix.
    if "," in spoken and ratio >= 0.12:
        first = spoken.split(",", 1)[0].strip()
        first_words = len(first.split())
        if 0 < first_words <= max(keep + 4, keep * 2):
            return first
    return " ".join(words[:keep])


def interrupt_metadata(
    *,
    reason: str,
    provider_response_id: str | None,
    audio_played_ms: int | None,
    generated_duration_ms: int | None,
    generated_text: str,
) -> dict[str, Any]:
    return {
        "interrupted": True,
        "interruption_reason": reason,
        "provider_response_id": provider_response_id,
        "audio_played_ms": audio_played_ms,
        "generated_duration_ms": generated_duration_ms,
        "generated_text": (generated_text or "")[:8000],
        "delivery": "interrupted",
    }


def _optional_int(value: Any) -> int | None:
    if value is None or value is False:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None or value is False:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
