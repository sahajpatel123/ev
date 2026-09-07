"""Audio normalization at the live client boundary.

The live Mac and web players share one contract: mono PCM16 with an explicit
sample rate. TTS providers may return WAV or compressed containers, so convert
those once on the backend instead of opening a second client playback engine.
"""

from __future__ import annotations

import array
import asyncio
import logging
import sys

logger = logging.getLogger("ev.voice.live.audio")

_RAW_PCM_TYPES = frozenset(
    {
        "audio/pcm",
        "audio/pcm16",
        "audio/l16",
        "audio/raw",
    }
)


def _content_type(value: str | None) -> str:
    return (value or "").split(";", 1)[0].strip().lower()


def _pcm16_from_container(audio: bytes) -> tuple[bytes, int]:
    """Decode any supported container through the existing audio boundary."""

    from app.voice.speaker import decode_waveform

    waveform, sample_rate = decode_waveform(audio)
    samples = array.array(
        "h",
        (
            int(round(max(-1.0, min(1.0, sample)) * (32768 if sample < 0 else 32767)))
            for sample in waveform
        ),
    )
    if sys.byteorder == "big":
        samples.byteswap()
    return samples.tobytes(), int(sample_rate)


async def normalize_live_audio(
    audio: bytes,
    *,
    content_type: str | None,
    sample_rate: int | None,
) -> tuple[bytes, str, int, int] | None:
    """Return ``(pcm16, type, rate, duration_ms)`` for one live TTS payload."""

    if not audio:
        return None
    kind = _content_type(content_type)
    if kind in _RAW_PCM_TYPES:
        pcm = audio[: len(audio) - (len(audio) % 2)]
        rate = int(sample_rate or 16_000)
    else:
        try:
            pcm, rate = await asyncio.wait_for(
                asyncio.to_thread(_pcm16_from_container, audio),
                timeout=15.0,
            )
        except Exception as exc:  # noqa: BLE001 - keep text/reply delivery alive
            logger.warning(
                "live TTS audio normalization failed type=%s error=%s",
                kind or "unknown",
                type(exc).__name__,
            )
            return None
    if not pcm or rate <= 0:
        return None
    duration_ms = int((len(pcm) / 2) * 1000 / rate)
    return pcm, "audio/pcm", rate, duration_ms
