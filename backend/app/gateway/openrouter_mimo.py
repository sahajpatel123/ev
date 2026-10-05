"""Xiaomi MiMo-V2.6-Flash via OpenRouter — Evie's single non-speech brain.

Measured live 2026-10-02 with the owner's key:
- multimodal input (text/image/audio/video) -> text, 1.05M context;
- ``reasoning``/``tools``/``structured_outputs`` supported;
- streaming first token ~1.1 s; ~$0.07-0.14 / 1M prompt tokens;
- ``usage.cost`` is the provider-reported receipt.

Speech stays ``gemini-3.8-live-extended-thinking``; this provider never handles the mouth.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from app.config import settings
from app.contracts import ChatMessage, ChatResult
from app.gateway.providers import OpenAICompatibleProvider

_ALLOWED_EFFORTS = {"low", "medium", "high"}


class MimoUnavailable(RuntimeError):
    """MiMo is unconfigured or returned an unusable response."""


class MimoEgressDenied(RuntimeError):
    """Remote chat egress is not permitted by the owner's configuration."""


class MimoProvider(OpenAICompatibleProvider):
    """OpenAI-compatible OpenRouter provider for MiMo-V2.6-Flash."""

    name = "mimo"
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
            default_model=(default_model or settings.mimo_model),
            provider_name="mimo",
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
            or getattr(settings, "mimo_reasoning_effort", None)
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
            raise MimoEgressDenied(
                "MiMo is blocked: EV_ALLOW_REMOTE_CHAT is not enabled"
            )
        if not (self.api_key or "").strip():
            raise MimoUnavailable(
                "MiMo is unavailable: EV_OPENROUTER_API_KEY is not set"
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

            logging.getLogger("ev.gateway.mimo").warning(
                "chat_egress consent record is absent; remote MiMo calls are "
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

        Callers read ``result.text``; MiMo returns the object as message
        content under a strict schema.
        """

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
                return ChatResult(
                    text=json.dumps(parsed, ensure_ascii=False, sort_keys=True),
                    tool_calls=list(result.tool_calls),
                    usage=result.usage,
                    model=result.model,
                )
        raise MimoUnavailable("MiMo did not return the requested structured object")
