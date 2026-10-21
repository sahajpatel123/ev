"""DeepSeek V4.1 Flash via OpenRouter — Evie's non-speech brain.

Mirrors :mod:`app.gateway.openrouter_mimo` (same OpenRouter chat-completions
protocol, tool calls, structured output, egress/key fail-closed gates):
only the provider name, model id, and settings keys differ. MiMo stays
registered as an explicit fallback; this provider owns the ``deepseek``
registry slot and the agent ``auto`` first pick.

The direct DeepSeek API (``api.deepseek.com``) remains available as the
``deepseek-direct`` registry entry
(:class:`app.gateway.cloud_providers.DeepSeekProvider`).

Speech stays ``gemini-3.8-live-extended-thinking``; this provider never handles the mouth.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Sequence
from typing import Any

from app.config import settings
from app.contracts import ChatMessage, ChatResult
from app.gateway.openrouter_mimo import MimoEgressDenied, MimoUnavailable
from app.gateway.providers import OpenAICompatibleProvider

logger = logging.getLogger("ev.gateway.deepseek")

DEFAULT_DEEPSEEK_FLASH_MODEL = "deepseek/deepseek-v4.1-flash"

_ALLOWED_EFFORTS = {"low", "medium", "high"}


class DeepSeekUnavailable(MimoUnavailable):
    """DeepSeek is unconfigured or returned an unusable response.

    Subclasses the MiMo errors so every existing fail-closed catch site
    (kernel turns, the coding lane, the FastAPI 503 handlers) keeps working
    unchanged; only the message names the new brain.
    """


class DeepSeekEgressDenied(MimoEgressDenied):
    """Remote chat egress is not permitted by the owner's configuration."""


_STRUCTURED_EMPTY_THRESHOLD = 3
_STRUCTURED_OUTAGE_COOLDOWN_SECONDS = 300.0


class _StructuredOutageBreaker:
    """Fast-fail for a dead structured-output adapter.

    Same policy as the MiMo breaker: after a few consecutive HTTP-200
    empty-content schema calls, fail fast for a cooldown and let callers
    fall back to plain chat immediately; a lone probe per cooldown detects
    recovery.
    """

    def __init__(self) -> None:
        self.streak = 0
        self.open_until = 0.0

    def reset(self) -> None:
        self.streak = 0
        self.open_until = 0.0

    def should_skip(self) -> bool:
        return (
            self.streak >= _STRUCTURED_EMPTY_THRESHOLD
            and time.monotonic() < self.open_until
        )

    def record_empty(self) -> None:
        self.streak += 1
        if self.streak >= _STRUCTURED_EMPTY_THRESHOLD:
            # Re-armed on every further empty too, so a lone probe failure
            # after cooldown extends the window instead of leaving it open.
            self.open_until = time.monotonic() + _STRUCTURED_OUTAGE_COOLDOWN_SECONDS
            if self.streak == _STRUCTURED_EMPTY_THRESHOLD:
                logger.warning(
                    "structured output empty %dx; fast-failing schema calls for %.0fs",
                    self.streak,
                    _STRUCTURED_OUTAGE_COOLDOWN_SECONDS,
                )

    def record_success(self) -> None:
        was_open = self.streak >= _STRUCTURED_EMPTY_THRESHOLD
        self.streak = 0
        self.open_until = 0.0
        if was_open:
            logger.warning("structured output recovered; schema calls resumed")


_STRUCTURED_BREAKER = _StructuredOutageBreaker()


def default_flash_model() -> str:
    """Configured DeepSeek main-brain model id (never empty)."""

    return (
        getattr(settings, "deepseek_flash_model", None)
        or settings.openrouter_model
        or DEFAULT_DEEPSEEK_FLASH_MODEL
    )


class DeepSeekFlashProvider(OpenAICompatibleProvider):
    """OpenAI-compatible OpenRouter provider for DeepSeek V4.1 Flash."""

    name = "deepseek"
    supports_media = True
    supports_tools = True

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
            default_model=(default_model or default_flash_model()),
            provider_name="deepseek",
        )
        # The kernel sets this per instance before its tool loop. Declare it
        # here so the capability guard recognizes the supported override.
        # None preserves the configured effort for non-kernel callers.
        self.reasoning_effort: str | None = None

    def _thinking_payload(self) -> dict | None:
        # No vendor thinking toggle: reasoning is requested through
        # ``reasoning.effort`` in ``_payload_extras``.
        return None

    def _payload_extras(self) -> dict:
        extras: dict[str, Any] = {
            "usage": {"include": True},
            "provider": {"sort": "throughput", "allow_fallbacks": True},
        }
        effort = (
            getattr(self, "reasoning_effort", None)
            or getattr(settings, "deepseek_reasoning_effort", None)
            or ""
        )
        effort = str(effort).strip().lower()
        if effort in _ALLOWED_EFFORTS:
            extras["reasoning"] = {"effort": effort}
        if effort == "low":
            # Conversational replies are short: time to the first token matters
            # more than sustained token throughput. Work retains throughput.
            extras["provider"]["sort"] = "latency"
        return extras

    async def _authorize(self) -> None:
        """Fail closed on the remote-egress gate and a missing key.

        The revocable ``chat_egress`` consent record is checked too; when it is
        absent the call proceeds but logs a warning so the gap stays visible
        instead of silent.
        """

        from app.compliance.policy import remote_processing_allowed

        if not remote_processing_allowed("chat_egress"):
            raise DeepSeekEgressDenied(
                "DeepSeek is blocked: EV_ALLOW_REMOTE_CHAT is not enabled"
            )
        if not (self.api_key or "").strip():
            raise DeepSeekUnavailable(
                "DeepSeek is unavailable: EV_OPENROUTER_API_KEY is not set"
            )
        try:
            from app.db import SessionLocal
            from app.training.consent import active_consent

            async with SessionLocal() as session:
                consent = await active_consent(session, "chat_egress")
        except Exception:  # noqa: BLE001 - consent lookup must not break chat
            consent = None
        if consent is None:
            import logging

            logging.getLogger("ev.gateway.deepseek").warning(
                "chat_egress consent record is absent; remote DeepSeek calls are "
                "covered only by EV_ALLOW_REMOTE_CHAT"
            )

    async def chat_structured(
        self,
        messages: Sequence[ChatMessage],
        *,
        schema: dict[str, Any],
        schema_name: str = "turn_intent",
        model: str | None = None,
        **_ignored: Any,
    ) -> ChatResult:
        """JSON-schema structured output, re-encoded as JSON text.

        Callers read ``result.text``; the model returns the object as message
        content under a strict schema.
        """

        if _STRUCTURED_BREAKER.should_skip():
            raise DeepSeekUnavailable(
                "DeepSeek structured output is in outage cooldown; use plain chat"
            )
        result = await self._complete(
            messages,
            model=model,
            temperature=0.2,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "schema": schema,
                    "strict": False,
                },
            },
        )
        text = (result.text or "").strip()
        if text:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                _STRUCTURED_BREAKER.record_success()
                return ChatResult(
                    text=json.dumps(parsed, ensure_ascii=False, sort_keys=True),
                    tool_calls=list(result.tool_calls),
                    usage=result.usage,
                    model=result.model,
                )
        _STRUCTURED_BREAKER.record_empty()
        raise DeepSeekUnavailable("DeepSeek did not return the requested structured object")
