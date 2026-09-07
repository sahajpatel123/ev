"""Muse identity, credentials, model selection, and call counters.

Muse Voice Transcribe is the Meta Model API ear. Muse Spark Contributor is the
OpenCode Go Responses brain. They use separate credentials and endpoints; this
module never logs or prints secret values.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from app.config import settings

META_API_BASE = "https://api.meta.ai/v1"
META_ASR_REALTIME_URL = "wss://api.meta.ai/v1/asr/realtime"
MUSE_SPARK_GO_BASE = "https://opencode.ai/zen/go/v1"

# Official public model ids from https://dev.meta.ai/docs/models.md
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
    """Return the OpenCode Go credential used by Spark Contributor.

    Muse Voice Transcribe and Muse Spark Contributor are different API
    surfaces. Voice keeps using the Meta Model API credential above, while
    Spark Contributor is served by OpenCode Go and therefore requires the
    dedicated ``OPENCODE_API_KEY`` credential. Keeping the credentials
    separate prevents a configured Meta ASR key from making Spark look
    healthy when the OpenCode integration has not been provisioned.
    """

    direct = (getattr(settings, "opencode_api_key", None) or "").strip()
    if direct:
        return direct
    for name in ("EV_OPENCODE_API_KEY", "OPENCODE_API_KEY"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    env_file = Path(
        str(getattr(settings, "opencode_env_file", "~/.config/ev/opencode.env"))
    ).expanduser()
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "OPENCODE_API_KEY" and value.strip():
                return value.strip().strip("'\"")
    except OSError:
        pass
    return ""


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


def muse_spark_base_url() -> str:
    """OpenCode Go Responses endpoint for Spark Contributor."""

    raw = (
        getattr(settings, "muse_spark_base_url", None)
        or os.environ.get("EV_MUSE_SPARK_BASE_URL")
        or ""
    ).strip()
    return raw.rstrip("/") or MUSE_SPARK_GO_BASE


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


def require_muse_spark_key(*, role: str = "Muse Spark") -> str:
    key = muse_spark_api_key()
    if not key:
        raise MuseProviderUnavailable(
            f"{role} is unavailable: OPENCODE_API_KEY is missing"
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
            _COUNTERS[key] = "" if key == "asr_last_error_class" else 0


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
