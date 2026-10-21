"""Which brain runs an agent goal — one resolver, explicit and network-free.

``EV_AGENT_PROVIDER`` (or an explicit argument) names the brain:
``auto`` | ``deepseek`` | ``openrouter`` | ``xai`` | ``deepseek-direct`` |
``openai`` | ``mimo`` | ``local`` | ``echo``.

``auto`` walks a fixed preference order and picks the first provider whose
credentials are present. The first pick is ``deepseek`` (DeepSeek V4.1 Flash
on OpenRouter, the main brain); every other candidate stays as fallback.
The choice is always reported back on the run receipt
(provider, model, whether it is the offline double), so nothing about the
run's provenance is hidden. Resolution never touches the network: a stale key
fails at call time with the provider's own typed error, which the loop turns
into an honest ``brain_unavailable`` receipt instead of a fake answer.

No silent fallback: if the explicitly named provider has no key, the resolver
returns that provider with ``available=False`` and the loop refuses to run.
Only ``auto`` falls through, and only to the offline double (``echo``), which
is reported as ``degraded=True``.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import settings

KNOWN_BRAINS = (
    "auto",
    "deepseek",
    "openrouter",
    "xai",
    "deepseek-direct",
    "openai",
    "mimo",
    "local",
    "echo",
)
AUTO_ORDER = ("deepseek", "openrouter", "xai", "deepseek-direct", "openai", "mimo", "local")


@dataclass(frozen=True)
class BrainChoice:
    provider: str
    model: str
    reason: str
    available: bool
    degraded: bool = False

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "reason": self.reason,
            "available": self.available,
            "degraded": self.degraded,
        }


def _key(name: str) -> str:
    return str(getattr(settings, name, "") or "").strip()


def _candidate(provider: str) -> BrainChoice:
    if provider == "openrouter":
        key = _key("openrouter_api_key")
        return BrainChoice(
            "openrouter", str(settings.openrouter_model or ""),
            "openrouter key present" if key else "EV_OPENROUTER_API_KEY is empty",
            bool(key),
        )
    if provider == "xai":
        key = _key("xai_api_key")
        return BrainChoice(
            "xai", str(settings.xai_model or ""), "xai key present" if key else "EV_XAI_API_KEY is empty",
            bool(key),
        )
    if provider == "deepseek":
        from app.gateway.openrouter_deepseek import default_flash_model

        key = _key("openrouter_api_key")
        return BrainChoice(
            "deepseek", default_flash_model(),
            "openrouter key present (deepseek main brain)"
            if key else "EV_OPENROUTER_API_KEY is empty",
            bool(key),
        )
    if provider == "deepseek-direct":
        key = _key("deepseek_api_key")
        return BrainChoice(
            "deepseek-direct", str(settings.deepseek_model or ""),
            "deepseek key present" if key else "EV_DEEPSEEK_API_KEY is empty",
            bool(key),
        )
    if provider == "openai":
        key = _key("agent_openai_api_key")
        return BrainChoice(
            "openai", str(settings.agent_openai_model or ""),
            "openai key present" if key else "EV_AGENT_OPENAI_API_KEY is empty",
            bool(key),
        )
    if provider == "mimo":
        key = _key("openrouter_api_key")
        return BrainChoice(
            "mimo", str(settings.mimo_model or ""),
            "openrouter key present" if key else "EV_OPENROUTER_API_KEY is empty",
            bool(key),
        )
    if provider == "local":
        base = str(getattr(settings, "local_model_base_url", "") or "").strip()
        return BrainChoice(
            "local", str(settings.local_model_name or ""),
            "local endpoint configured" if base else "EV_LOCAL_MODEL_BASE_URL is empty",
            bool(base),
        )
    return BrainChoice("echo", "echo-local", "offline double (no brain key configured)", True, degraded=True)


def resolve_agent_brain(preferred: str | None = None) -> BrainChoice:
    """Resolve the agent's brain without touching the network."""
    name = str(preferred or settings.agent_provider or "auto").strip().lower()
    if name not in KNOWN_BRAINS:
        known = ", ".join(KNOWN_BRAINS)
        raise ValueError(f"unknown agent brain {name!r}; known: {known}")
    if name != "auto":
        return _candidate(name)
    for candidate in AUTO_ORDER:
        choice = _candidate(candidate)
        if choice.available:
            return choice
    return _candidate("echo")


def build_agent_provider(choice: BrainChoice):
    """Instantiate the chat provider for one resolution. Raises on no key."""
    if choice.provider == "openrouter":
        from app.gateway.cloud_providers import OpenRouterProvider

        return OpenRouterProvider()
    if choice.provider == "xai":
        from app.gateway.cloud_providers import XaiProvider

        return XaiProvider()
    if choice.provider == "deepseek":
        from app.gateway.openrouter_deepseek import DeepSeekFlashProvider

        return DeepSeekFlashProvider()
    if choice.provider == "deepseek-direct":
        from app.gateway.cloud_providers import DeepSeekProvider

        return DeepSeekProvider()
    if choice.provider == "openai":
        from app.gateway.cloud_providers import OpenAIChatProvider

        return OpenAIChatProvider()
    if choice.provider == "mimo":
        from app.gateway.openrouter_mimo import MimoProvider

        return MimoProvider()
    if choice.provider == "local":
        from app.gateway.providers import OpenAICompatibleProvider

        return OpenAICompatibleProvider(
            base_url=str(settings.local_model_base_url or ""),
            api_key="local",
            default_model=str(settings.local_model_name or "llama3"),
            provider_name="local",
        )
    from app.gateway.providers import EchoProvider

    return EchoProvider()


def brain_status() -> dict:
    """Doctor-facing snapshot: every candidate and the auto pick."""
    return {
        "provider_setting": str(settings.agent_provider or "auto"),
        "auto_pick": resolve_agent_brain().to_dict(),
        "candidates": {name: _candidate(name).to_dict() for name in AUTO_ORDER},
    }
