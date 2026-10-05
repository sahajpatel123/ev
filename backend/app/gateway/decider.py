"""Supervisor verdict model via OpenRouter — same key as MiMo, different model id.

The decider (`perplexity/pplx-decider-v1-27b` by default) never talks to the
owner and never executes tools. It reads one node plus its worker receipt and
returns a strict verdict object. Verdicts interpret evidence; they never
substitute for it — a worker receipt without evidence refs is never accepted,
whatever the model says.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.config import settings
from app.contracts import ChatMessage, ChatResult
from app.gateway.providers import OpenAICompatibleProvider

_ALLOWED_EFFORTS = {"low", "medium", "high"}


class DeciderUnavailable(RuntimeError):
    """The decider model is unconfigured or returned an unusable response."""


class DeciderEgressDenied(RuntimeError):
    """Remote verdict calls are not permitted by the owner's configuration."""


class DeciderProvider(OpenAICompatibleProvider):
    """OpenAI-compatible OpenRouter provider for the supervisor verdict model."""

    name = "decider"
    supports_media = False
    supports_tools = False

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        default_model: str | None = None,
    ) -> None:
        super().__init__(
            base_url=(base_url or settings.openrouter_base_url),
            api_key=(api_key if api_key is not None else settings.openrouter_api_key),
            default_model=(default_model or settings.decider_model),
            provider_name="decider",
        )
        self.reasoning_effort: str | None = None

    def _payload_extras(self) -> dict[str, Any]:
        extras: dict[str, Any] = {
            "usage": {"include": True},
            "provider": {"sort": "latency", "allow_fallbacks": True},
        }
        effort = (
            getattr(self, "reasoning_effort", None)
            or getattr(settings, "decider_reasoning_effort", None)
            or ""
        )
        effort = str(effort).strip().lower()
        if effort in _ALLOWED_EFFORTS:
            extras["reasoning"] = {"effort": effort}
        return extras

    async def _authorize(self) -> None:
        """Fail closed on the shared remote-egress gate and a missing key."""

        from app.compliance.policy import remote_processing_allowed

        if not remote_processing_allowed("chat_egress"):
            raise DeciderEgressDenied(
                "Decider is blocked: EV_ALLOW_REMOTE_CHAT is not enabled"
            )
        if not (self.api_key or "").strip():
            raise DeciderUnavailable(
                "Decider is unavailable: EV_OPENROUTER_API_KEY is not set"
            )

    async def judge(
        self,
        messages: Sequence[ChatMessage],
        *,
        schema: dict[str, Any],
        schema_name: str = "supervisor_verdict",
        model: str | None = None,
    ) -> ChatResult:
        """One strict-schema verdict call; raw text is never trusted."""

        if not hasattr(self, "chat_structured"):
            raise DeciderUnavailable("Decider provider lacks structured output")
        try:
            return await self.chat_structured(
                messages, schema=schema, schema_name=schema_name, model=model
            )
        except RuntimeError as exc:
            raise DeciderUnavailable(str(exc)) from exc


def decider_available() -> bool:
    """Cheap, network-free pre-check that the verdict model can serve.

    Same key as the MiMo text brain: without it the supervisor falls back to
    the deterministic local verdict instead of treating a guess as a judgment.
    """

    key = (getattr(settings, "openrouter_api_key", None) or "").strip()
    model = (getattr(settings, "decider_model", None) or "").strip()
    return bool(key and model)
