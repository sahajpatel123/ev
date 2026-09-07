"""Central model-role configuration (G1.3).

Voice / Turn-Control / Manager are one configuration decision, not scattered
provider/model IDs.  This module is the single authority that the rest of
Evie reads.

    VOICE_MODEL  → gpt-realtime-2.1-mini  (live audio, provider: openai-realtime)
    TURN_MODEL   → gpt-5.6-luna           (text control plane, provider: openai)
    MANAGER_MODEL→ deepseek-v4-flash      (complex work, provider: deepseek)

All three resolve from `app.config.settings`; changing a model is a config
change, not a code change.  Health and cost tracking are exposed here so
Mission Control / Self Diagnostics can consume them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.config import settings

ModelRole = Literal["voice", "turn_control", "manager"]

@dataclass(frozen=True)
class ModelInfo:
    role: ModelRole
    provider: str
    model: str
    base_url: str | None = None
    available: bool = False
    last_error: str | None = None


def voice_model_info() -> ModelInfo:
    from app.gateway.muse import (
        muse_api_key,
        muse_asr_realtime_url,
        muse_hearing_active,
        muse_voice_model,
    )

    if muse_hearing_active():
        return ModelInfo(
            role="voice",
            provider="meta_muse_voice",
            model=muse_voice_model(),
            base_url=muse_asr_realtime_url(),
            available=bool(muse_api_key()),
        )
    from app.voice.live.grok_voice import live_realtime_provider

    live = live_realtime_provider()
    if live == "xai":
        return ModelInfo(
            role="voice",
            provider="xai-realtime",
            model=(settings.xai_voice_model or "grok-voice-think-fast-2.0").strip(),
            base_url=(settings.xai_voice_realtime_url or "").strip(),
            available=bool((settings.xai_api_key or "").strip()),
        )
    if live == "openai":
        return ModelInfo(
            role="voice",
            provider="openai-realtime",
            model=(settings.openai_realtime_model or "gpt-realtime-2.1-mini").strip(),
            base_url=(settings.openai_realtime_url or "wss://api.openai.com/v1/realtime").strip(),
            available=bool((settings.openai_api_key or "").strip()),
        )
    return ModelInfo(
        role="voice",
        provider=(settings.voice_asr_provider or "pipeline").strip() or "pipeline",
        model=(settings.voice_asr_model or "").strip() or "pipeline",
        base_url=None,
        available=True,
    )


def turn_control_model_info() -> ModelInfo:
    from app.gateway.muse import (
        muse_intelligence_active,
        muse_spark_base_url,
        muse_spark_key_loaded,
        muse_spark_model,
    )

    if muse_intelligence_active():
        return ModelInfo(
            role="turn_control",
            provider="meta_muse_spark",
            model=muse_spark_model(),
            base_url=muse_spark_base_url(),
            available=muse_spark_key_loaded(),
        )
    raw = (getattr(settings, "turn_control_model", None) or getattr(settings, "openai_chat_model", None) or "gpt-5.6-luna").strip()
    provider = (getattr(settings, "turn_control_provider", None) or "openai").strip() or "openai"
    base_url = (getattr(settings, "openai_base_url", None) or "https://api.openai.com/v1").strip()
    available = bool((settings.openai_api_key or "").strip())
    return ModelInfo(
        role="turn_control",
        provider=provider,
        model=raw,
        base_url=base_url,
        available=available,
    )


def manager_model_info() -> ModelInfo:
    from app.gateway.muse import (
        muse_intelligence_active,
        muse_spark_base_url,
        muse_spark_key_loaded,
        muse_spark_model,
    )

    if muse_intelligence_active():
        return ModelInfo(
            role="manager",
            provider="meta_muse_spark",
            model=muse_spark_model(),
            base_url=muse_spark_base_url(),
            available=muse_spark_key_loaded(),
        )
    return ModelInfo(
        role="manager",
        provider="deepseek",
        model=(settings.deepseek_model or "deepseek-v4-flash").strip(),
        base_url=(settings.deepseek_base_url or "https://api.deepseek.com").strip(),
        available=bool((settings.deepseek_api_key or "").strip()),
    )


def all_models() -> dict[str, ModelInfo]:
    return {
        "voice": voice_model_info(),
        "turn_control": turn_control_model_info(),
        "manager": manager_model_info(),
    }


def health_snapshot() -> dict:
    """Model health for /v1/health and Mission Control."""
    from app.gateway.muse import muse_counters_snapshot, muse_spark_reasoning_effort

    infos = all_models()
    return {
        "voice": {
            "provider": infos["voice"].provider,
            "model": infos["voice"].model,
            "available": infos["voice"].available,
        },
        "turn_control": {
            "provider": infos["turn_control"].provider,
            "model": infos["turn_control"].model,
            "available": infos["turn_control"].available,
        },
        "manager": {
            "provider": infos["manager"].provider,
            "model": infos["manager"].model,
            "available": infos["manager"].available,
        },
        "muse": {
            "reasoning_effort": muse_spark_reasoning_effort(),
            **muse_counters_snapshot(),
        },
    }
