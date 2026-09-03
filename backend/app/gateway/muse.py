"""Muse / Meta Model API shared identity, secret access, and call counters.

Hearing (Muse Voice Transcribe) and intelligence (Muse Spark) share one
credential. This module never logs, prints, or returns the secret value.
"""

from __future__ import annotations

import threading
from typing import Any

from app.config import settings

META_API_BASE = "https://api.meta.ai/v1"
META_ASR_REALTIME_URL = "wss://api.meta.ai/v1/asr/realtime"

# Official public model ids from https://dev.meta.ai/docs/models.md
MUSE_VOICE_MODEL = "muse-voice-transcribe-1.0"
MUSE_SPARK_MODEL = "muse-spark-1.3-contributor"

MUSE_SPARK_PROVIDERS = frozenset({"meta_muse_spark", "muse", "muse_spark"})
MUSE_VOICE_PROVIDERS = frozenset({"meta_muse_voice", "muse_voice"})

_LOCK = threading.Lock()
_COUNTERS: dict[str, Any] = {
    "spark_calls": 0,
    "spark_input_tokens": 0,
    "spark_output_tokens": 0,
    "spark_cached_tokens": 0,
    "spark_reasoning_tokens": 0,
    "voice_calls": 0,
    "voice_audio_ms": 0,
}


class MuseProviderUnavailable(RuntimeError):
    """Muse credential missing or the Meta Model API refused the call."""


def muse_api_key() -> str:
    """Return the canonical Meta Model API credential, or empty.

    Accepts ``META_MODEL_API_KEY`` (overlay) and ``EV_META_MODEL_API_KEY``.
    Never log the return value.
    """

    direct = (getattr(settings, "meta_model_api_key", None) or "").strip()
    if direct:
        return direct
    import os

    return (
        (os.environ.get("EV_META_MODEL_API_KEY") or "").strip()
        or (os.environ.get("META_MODEL_API_KEY") or "").strip()
        or (os.environ.get("MODEL_API_KEY") or "").strip()
    )


def muse_key_loaded() -> bool:
    return bool(muse_api_key())


def muse_spark_model() -> str:
    raw = (getattr(settings, "muse_spark_model", None) or "").strip()
    return raw or MUSE_SPARK_MODEL


def muse_voice_model() -> str:
    raw = (getattr(settings, "muse_voice_model", None) or "").strip()
    return raw or MUSE_VOICE_MODEL


def muse_spark_reasoning_effort() -> str:
    raw = (getattr(settings, "muse_spark_reasoning_effort", None) or "high").strip().lower()
    return raw or "high"


def muse_base_url() -> str:
    raw = (getattr(settings, "meta_model_base_url", None) or "").strip()
    return raw.rstrip("/") or META_API_BASE


def muse_asr_realtime_url() -> str:
    raw = (getattr(settings, "meta_model_asr_realtime_url", None) or "").strip()
    return raw.rstrip("/") or META_ASR_REALTIME_URL


def configured_intelligence_provider() -> str:
    """Primary general-intelligence provider name.

    Any Muse Spark slot wins over leftover xAI / DeepSeek / OpenAI values in
    the other slots. Explicit rollback requires clearing Muse from
    intelligence, chat, and turn-control — not leaving one Muse flag on.
    """

    intel = (getattr(settings, "intelligence_provider", None) or "").strip()
    chat = (settings.chat_provider or "").strip()
    turn = (getattr(settings, "turn_control_provider", None) or "").strip()
    for name in (intel, chat, turn):
        if name.lower() in MUSE_SPARK_PROVIDERS:
            return name
    return intel or chat


def muse_intelligence_active() -> bool:
    names = {
        configured_intelligence_provider().lower(),
        (settings.chat_provider or "").strip().lower(),
        (getattr(settings, "turn_control_provider", None) or "").strip().lower(),
    }
    return bool(names & MUSE_SPARK_PROVIDERS)


def muse_hearing_active() -> bool:
    name = (settings.voice_asr_provider or "").strip().lower()
    return name in MUSE_VOICE_PROVIDERS


def require_muse_key(*, role: str) -> str:
    key = muse_api_key()
    if not key:
        raise MuseProviderUnavailable(
            f"{role} is unavailable: META_MODEL_API_KEY is missing"
        )
    return key


def note_spark_call(*, usage: dict | None = None, model: str | None = None) -> None:
    del model  # identity is config; never put secrets or prompts here
    usage = usage or {}
    prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    cached = int(details.get("cached_tokens") or usage.get("cached_tokens") or 0)
    out_details = usage.get("completion_tokens_details") if isinstance(usage.get("completion_tokens_details"), dict) else {}
    reasoning = int(out_details.get("reasoning_tokens") or usage.get("reasoning_tokens") or 0)
    with _LOCK:
        _COUNTERS["spark_calls"] += 1
        _COUNTERS["spark_input_tokens"] += prompt
        _COUNTERS["spark_output_tokens"] += completion
        _COUNTERS["spark_cached_tokens"] += cached
        _COUNTERS["spark_reasoning_tokens"] += reasoning


def note_voice_call(*, audio_ms: int = 0) -> None:
    with _LOCK:
        _COUNTERS["voice_calls"] += 1
        _COUNTERS["voice_audio_ms"] += max(0, int(audio_ms))


def muse_counters_snapshot() -> dict[str, Any]:
    with _LOCK:
        audio_ms = int(_COUNTERS["voice_audio_ms"])
        return {
            "spark_calls": int(_COUNTERS["spark_calls"]),
            "spark_input_tokens": int(_COUNTERS["spark_input_tokens"]),
            "spark_output_tokens": int(_COUNTERS["spark_output_tokens"]),
            "spark_cached_tokens": int(_COUNTERS["spark_cached_tokens"]),
            "spark_reasoning_tokens": int(_COUNTERS["spark_reasoning_tokens"]),
            "voice_calls": int(_COUNTERS["voice_calls"]),
            "voice_audio_ms": audio_ms,
            "voice_audio_minutes": round(audio_ms / 60000.0, 4),
        }


def reset_muse_counters() -> None:
    with _LOCK:
        for key in list(_COUNTERS):
            _COUNTERS[key] = 0


def refuse_legacy_cloud_brain(name: str | None = None) -> None:
    """Block leftover OpenAI / xAI / DeepSeek / OpenCode brains while Muse is on.

    Spark itself is allowed. A missing name is treated as a legacy caller
    (Luna Responses, raw chat completions) and is refused.
    """

    if not muse_intelligence_active():
        return
    label = (name or "").strip().lower()
    if label in MUSE_SPARK_PROVIDERS:
        return
    raise MuseProviderUnavailable(
        "legacy cloud brain is blocked while Muse Spark is the general intelligence"
    )
