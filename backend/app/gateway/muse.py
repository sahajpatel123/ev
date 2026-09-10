"""Muse identity, credentials, model selection, and call counters.

Muse Voice Transcribe and Muse Spark Contributor both use the official Meta
Model API. This module never logs or prints secret values.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from app.config import settings

META_API_BASE = "https://api.meta.ai/v1"
META_ASR_REALTIME_URL = "wss://api.meta.ai/v1/asr/realtime"
# Legacy OpenCode Zen Spark URL. Cognitive OS must never use this as the
# inference destination; muse_spark_base_url() remaps it to META_API_BASE.
MUSE_SPARK_GO_BASE = "https://opencode.ai/zen/go/v1"

# Official public model ids from https://ai.developer.meta.com/docs/models.md
MUSE_VOICE_MODEL = "muse-voice-transcribe-1.0"
# Evie is intentionally pinned to the exact Contributor model selected by the
# owner. Do not silently drift to a different Spark slot when a stale .env
# survives a migration.
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
    "spark_zen_calls": 0,
    "spark_meta_calls": 0,
    "spark_destination": "",
    "voice_calls": 0,
    "voice_audio_ms": 0,
    "asr_sessions_opened": 0,
    "asr_sessions_completed": 0,
    "asr_sessions_failed": 0,
    "asr_audio_bytes_sent": 0,
    "asr_client_pcm_bytes": 0,
    "asr_keepalive_bytes": 0,
    "asr_partials_received": 0,
    "asr_finals_received": 0,
    "asr_last_error_class": "",
    "asr_last_client_rms": 0,
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


def muse_spark_api_key() -> str:
    """Spark Contributor uses the official Meta Model API credential.

    OpenCode Zen is not a cognitive inference route. Voice Transcribe and Spark
    share ``META_MODEL_API_KEY`` / ``EV_META_MODEL_API_KEY`` / ``MODEL_API_KEY``.
    """

    return muse_api_key()


def muse_spark_key_loaded() -> bool:
    return bool(muse_spark_api_key())


def muse_spark_model() -> str:
    raw = (getattr(settings, "muse_spark_model", None) or "").strip()
    # Only the exact requested Contributor slot is valid. This guards a stale
    # operator setting from changing the project model without an explicit
    # code/config migration.
    if raw == MUSE_SPARK_MODEL:
        return raw
    return MUSE_SPARK_MODEL


def _is_zen_spark_url(raw: str) -> bool:
    low = (raw or "").strip().lower()
    return "opencode.ai" in low or "/zen/" in low


def muse_spark_base_url() -> str:
    """Official Meta Model API Responses base for Spark Contributor."""

    raw = (
        getattr(settings, "muse_spark_base_url", None)
        or os.environ.get("EV_MUSE_SPARK_BASE_URL")
        or ""
    ).strip().rstrip("/")
    if not raw or _is_zen_spark_url(raw):
        return META_API_BASE.rstrip("/")
    return raw


def muse_spark_inference_route() -> str:
    url = muse_spark_base_url().lower()
    if "api.meta.ai" in url:
        return "meta_model_api"
    if _is_zen_spark_url(url):
        return "opencode_zen"
    return "other"


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

    Cognitive OS V2: when the kernel mode is on, Spark is the one mind even
    when every slot still names a legacy brain — the leftovers are refused
    (refuse_legacy_cloud_brain) instead of silently serving. Rollback to the
    legacy split is EV_COGNITIVE_MODE=legacy_mini.
    """
    from app.cognitive.mode import muse_kernel_active

    if muse_kernel_active():
        return "meta_muse_spark"
    intel = (getattr(settings, "intelligence_provider", None) or "").strip()
    chat = (settings.chat_provider or "").strip()
    turn = (getattr(settings, "turn_control_provider", None) or "").strip()
    for name in (intel, chat, turn):
        if name.lower() in MUSE_SPARK_PROVIDERS:
            return name
    return intel or chat


def muse_intelligence_active() -> bool:
    """Slot-driven Muse activation — voice data-plane semantics.

    The S2S mouth gate (grok_voice.live_realtime_provider), the TTS mouth
    lock (voice/tts.py), and the phone media gate (webrtc_live) key on this:
    kernel mode alone must never rewire how Evie hears or speaks. Thinking
    surfaces use muse_brain_active() instead.
    """
    intel = (getattr(settings, "intelligence_provider", None) or "").strip()
    chat = (settings.chat_provider or "").strip()
    turn = (getattr(settings, "turn_control_provider", None) or "").strip()
    names = {intel.lower(), chat.lower(), turn.lower()}
    return bool(names & MUSE_SPARK_PROVIDERS)


def muse_brain_active() -> bool:
    """Spark is the one mind: explicit Muse slots OR Cognitive OS kernel mode."""
    from app.cognitive.mode import muse_kernel_active

    return muse_intelligence_active() or muse_kernel_active()


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


def require_muse_spark_key(*, role: str = "Muse Spark") -> str:
    key = muse_spark_api_key()
    if not key:
        raise MuseProviderUnavailable(
            f"{role} is unavailable: META_MODEL_API_KEY is missing"
        )
    return key


def note_spark_call(
    *,
    usage: dict | None = None,
    model: str | None = None,
    destination: str | None = None,
) -> None:
    del model  # identity is config; never put secrets or prompts here
    usage = usage or {}
    prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    cached = int(details.get("cached_tokens") or usage.get("cached_tokens") or 0)
    out_details = usage.get("completion_tokens_details") if isinstance(usage.get("completion_tokens_details"), dict) else {}
    reasoning = int(out_details.get("reasoning_tokens") or usage.get("reasoning_tokens") or 0)
    dest = (destination or muse_spark_base_url() or "").strip().lower()
    with _LOCK:
        _COUNTERS["spark_calls"] += 1
        _COUNTERS["spark_input_tokens"] += prompt
        _COUNTERS["spark_output_tokens"] += completion
        _COUNTERS["spark_cached_tokens"] += cached
        _COUNTERS["spark_reasoning_tokens"] += reasoning
        _COUNTERS["spark_destination"] = dest[:160]
        if _is_zen_spark_url(dest):
            _COUNTERS["spark_zen_calls"] += 1
        if "api.meta.ai" in dest:
            _COUNTERS["spark_meta_calls"] += 1


def note_voice_call(*, audio_ms: int = 0) -> None:
    with _LOCK:
        _COUNTERS["voice_calls"] += 1
        _COUNTERS["voice_audio_ms"] += max(0, int(audio_ms))


def note_asr_session_opened() -> None:
    with _LOCK:
        _COUNTERS["asr_sessions_opened"] += 1


def note_asr_session_completed() -> None:
    with _LOCK:
        _COUNTERS["asr_sessions_completed"] += 1


def note_asr_session_failed(*, error_class: str = "") -> None:
    with _LOCK:
        _COUNTERS["asr_sessions_failed"] += 1
        if error_class:
            _COUNTERS["asr_last_error_class"] = str(error_class)[:80]


def note_asr_audio_bytes(n: int) -> None:
    with _LOCK:
        _COUNTERS["asr_audio_bytes_sent"] += max(0, int(n))


def note_asr_client_pcm(n: int, rms: float) -> None:
    with _LOCK:
        _COUNTERS["asr_client_pcm_bytes"] += max(0, int(n))
        _COUNTERS["asr_last_client_rms"] = round(float(rms), 1)


def note_asr_keepalive_bytes(n: int) -> None:
    with _LOCK:
        _COUNTERS["asr_keepalive_bytes"] += max(0, int(n))


def note_asr_partial() -> None:
    with _LOCK:
        _COUNTERS["asr_partials_received"] += 1


def note_asr_final() -> None:
    with _LOCK:
        _COUNTERS["asr_finals_received"] += 1


def muse_counters_snapshot() -> dict[str, Any]:
    with _LOCK:
        audio_ms = int(_COUNTERS["voice_audio_ms"])
        return {
            "spark_calls": int(_COUNTERS["spark_calls"]),
            "spark_input_tokens": int(_COUNTERS["spark_input_tokens"]),
            "spark_output_tokens": int(_COUNTERS["spark_output_tokens"]),
            "spark_cached_tokens": int(_COUNTERS["spark_cached_tokens"]),
            "spark_reasoning_tokens": int(_COUNTERS["spark_reasoning_tokens"]),
            "spark_zen_calls": int(_COUNTERS["spark_zen_calls"]),
            "spark_meta_calls": int(_COUNTERS["spark_meta_calls"]),
            "spark_destination": str(_COUNTERS["spark_destination"] or ""),
            "spark_route": muse_spark_inference_route(),
            "voice_calls": int(_COUNTERS["voice_calls"]),
            "voice_audio_ms": audio_ms,
            "voice_audio_minutes": round(audio_ms / 60000.0, 4),
            "asr_sessions_opened": int(_COUNTERS["asr_sessions_opened"]),
            "asr_sessions_completed": int(_COUNTERS["asr_sessions_completed"]),
            "asr_sessions_failed": int(_COUNTERS["asr_sessions_failed"]),
            "asr_audio_bytes_sent": int(_COUNTERS["asr_audio_bytes_sent"]),
            "asr_client_pcm_bytes": int(_COUNTERS["asr_client_pcm_bytes"]),
            "asr_keepalive_bytes": int(_COUNTERS["asr_keepalive_bytes"]),
            "asr_last_client_rms": float(_COUNTERS["asr_last_client_rms"] or 0),
            "asr_partials_received": int(_COUNTERS["asr_partials_received"]),
            "asr_finals_received": int(_COUNTERS["asr_finals_received"]),
            "asr_last_error_class": str(_COUNTERS["asr_last_error_class"] or ""),
        }


def reset_muse_counters() -> None:
    with _LOCK:
        for key in list(_COUNTERS):
            if key in {"asr_last_error_class", "spark_destination"}:
                _COUNTERS[key] = ""
            else:
                _COUNTERS[key] = 0


def refuse_legacy_cloud_brain(name: str | None = None) -> None:
    """Block leftover OpenAI / xAI / DeepSeek / OpenCode brains while Muse is on.

    Spark itself is allowed. A missing name is treated as a legacy caller
    (Luna Responses, raw chat completions) and is refused.
    """

    if not muse_brain_active():
        return
    label = (name or "").strip().lower()
    if label in MUSE_SPARK_PROVIDERS:
        return
    raise MuseProviderUnavailable(
        "legacy cloud brain is blocked while Muse Spark is the general intelligence"
    )
