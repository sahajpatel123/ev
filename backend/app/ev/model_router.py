"""Central model-role configuration (G1.3 + Cognitive OS V2).

Role topology (owner-ordered, two models only):

- voice (mouth only)  -> gemini-3.8-live-extended-thinking (gemini-live S2S)
- turn_control        -> settings.brain_model (Muse Spark, openrouter)
- manager             -> settings.brain_model (same single brain)
- code jobs           -> settings.brain_model (same single brain)

All resolve from `app.config.settings`; changing a model is a config
change, not a code change. Health and cost tracking are exposed here so
Mission Control / Self Diagnostics can consume them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.config import settings

ModelRole = Literal["voice", "turn_control", "manager"]

_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_GEMINI_LIVE_URL = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"


@dataclass(frozen=True)
class ModelInfo:
    role: ModelRole
    provider: str
    model: str
    base_url: str | None = None
    available: bool = False
    last_error: str | None = None


def voice_model_info() -> ModelInfo:
    from app.voice.live.gemini_live import live_speech_provider

    live = live_speech_provider()
    if live == "gemini":
        return ModelInfo(
            role="voice",
            provider="gemini-live",
            model=(settings.gemini_live_model or "gemini-3.8-live-extended-thinking").strip(),
            base_url=(settings.gemini_live_url or _GEMINI_LIVE_URL).strip(),
            available=bool((settings.google_api_key or "").strip()),
        )
    return ModelInfo(
        role="voice",
        provider=(settings.voice_asr_provider or "pipeline").strip() or "pipeline",
        model=(settings.voice_asr_model or "").strip() or "pipeline",
        base_url=None,
        available=True,
    )


def _brain_info(role: ModelRole) -> ModelInfo:
    from app.gateway.roles import resolve_text_brain, text_role_available

    brain = resolve_text_brain()
    return ModelInfo(
        role=role,
        provider=brain.provider,
        model=brain.model,
        base_url=(settings.openrouter_base_url or _OPENROUTER_BASE_URL).strip(),
        available=text_role_available(),
    )


def turn_control_model_info() -> ModelInfo:
    return _brain_info("turn_control")


def manager_model_info() -> ModelInfo:
    return _brain_info("manager")


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
        "brain": {
            "reasoning_effort": (
                settings.brain_reasoning_effort or "medium"
            ).strip(),
            "enabled": infos["manager"].available,
        },
    }
