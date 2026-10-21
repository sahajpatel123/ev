"""Cloud chat providers that share the OpenAI-compatible protocol.

Kept apart from :mod:`app.gateway.providers` so the registry can import
these lazily and the offline doubles stay dependency-free. Every provider
fails closed on the remote-egress gate and a missing key — an agent run
never silently degrades to canned prose and calls it thinking.

These are *chat brains only*: the agent runtime uses them for tool calls.
Speech stays with the voice coprocessor.
"""

from __future__ import annotations

from app.config import settings
from app.gateway.providers import OpenAICompatibleProvider


class CloudChatEgressDenied(RuntimeError):
    """Remote chat egress is not permitted by the owner's configuration."""


class CloudChatUnavailable(RuntimeError):
    """The provider is unconfigured (no API key) or unreachable."""


class _CloudChatProvider(OpenAICompatibleProvider):
    """Shared egress + key gate for the agent's cloud brains."""

    env_hint = ""

    async def _authorize(self) -> None:
        from app.compliance.policy import remote_processing_allowed

        if not remote_processing_allowed("chat_egress"):
            raise CloudChatEgressDenied(
                f"{self.name} is blocked: EV_ALLOW_REMOTE_CHAT is not enabled"
            )
        if not (self.api_key or "").strip():
            raise CloudChatUnavailable(
                f"{self.name} is unavailable: {self.env_hint} is not set"
            )


class OpenRouterProvider(_CloudChatProvider):
    """An OpenRouter cloud brain with native tool calling.

    Default is ``deepseek/deepseek-v4.1-flash`` (1M context, tools). The
    provider is a configuration seam: changing ``EV_OPENROUTER_MODEL`` swaps
    the mind without touching the kernel, agent loop, or tools. The agent
    ``auto`` first pick is the ``deepseek`` provider
    (``app.gateway.openrouter_deepseek``); this entry stays as an explicit
    fallback with its own model setting.
    """

    name = "openrouter"
    env_hint = "EV_OPENROUTER_API_KEY"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        default_model: str | None = None,
    ) -> None:
        super().__init__(
            base_url=(base_url or settings.openrouter_base_url or "https://openrouter.ai/api/v1"),
            api_key=api_key if api_key is not None else settings.openrouter_api_key,
            default_model=(
                default_model or settings.openrouter_model or "deepseek/deepseek-v4.1-flash"
            ),
            provider_name="openrouter",
        )


class XaiProvider(_CloudChatProvider):
    """Grok chat completions (xAI). Tool calling supported."""

    name = "xai"
    env_hint = "EV_XAI_API_KEY"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        default_model: str | None = None,
    ) -> None:
        super().__init__(
            base_url=(base_url or settings.xai_base_url or "https://api.x.ai/v1"),
            api_key=api_key if api_key is not None else settings.xai_api_key,
            default_model=(default_model or settings.xai_model or "grok-4-1-fast-non-reasoning"),
            provider_name="xai",
        )


class DeepSeekProvider(_CloudChatProvider):
    """DeepSeek chat completions via the DeepSeek API directly.

    Tool calling supported. Registered as ``deepseek-direct``; the
    ``deepseek`` slot is the OpenRouter DeepSeek V4.1 Flash main brain.
    """

    name = "deepseek"
    env_hint = "EV_DEEPSEEK_API_KEY"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        default_model: str | None = None,
    ) -> None:
        super().__init__(
            base_url=(base_url or settings.deepseek_base_url or "https://api.deepseek.com/v1"),
            api_key=api_key if api_key is not None else settings.deepseek_api_key,
            default_model=(default_model or settings.deepseek_model or "deepseek-chat"),
            provider_name="deepseek",
        )


class OpenAIChatProvider(_CloudChatProvider):
    """OpenAI chat completions for the agent role.

    Uses its own ``agent_openai_*`` settings so the realtime voice path keeps
    its existing ``EV_OPENAI_*`` keys untouched.
    """

    name = "openai"
    env_hint = "EV_AGENT_OPENAI_API_KEY"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        default_model: str | None = None,
    ) -> None:
        super().__init__(
            base_url=(base_url or settings.agent_openai_base_url or "https://api.openai.com/v1"),
            api_key=api_key if api_key is not None else settings.agent_openai_api_key,
            default_model=(default_model or settings.agent_openai_model or "gpt-5.2"),
            provider_name="openai",
        )
