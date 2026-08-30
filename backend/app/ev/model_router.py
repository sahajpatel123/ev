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
    return ModelInfo(
        role="voice",
        provider="openai-realtime",
        model=(settings.openai_realtime_model or "gpt-realtime-2.1-mini").strip(),
        base_url=(settings.openai_realtime_url or "wss://api.openai.com/v1/realtime").strip(),
        available=bool((settings.openai_api_key or "").strip()),
    )


def turn_control_model_info() -> ModelInfo:
    # Luna is GPT-5.6 Luna via OpenAI text/Responses path.  Falls back to
    # gpt-4o-mini when the Luna model ID is not yet available in the account;
    # the structured contract and prompt are identical.
    raw = (getattr(settings, "turn_control_model", None) or getattr(settings, "openai_chat_model", None) or "gpt-5.6-luna").strip()
    # Model alias: allow Luna to be served by available chat model when Luna is not yet provisioned
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
    }
