"""Muse Spark via OpenRouter — Evie's single non-speech brain.

The brain is one model on one route: ``settings.brain_model`` (default
``meta/muse-spark-1.3-contributor``) through OpenRouter's chat-completions
API. Speech is the other model, ``gemini-3.8-live-extended-thinking`` on
Google AI Studio; this provider never handles the mouth.

Model facts, read from OpenRouter's /api/v1/models listing on 2026-10-25:
text/image/video/file input (no audio), text output, 1,048,576-token
context, ``tools`` / ``reasoning`` / ``structured_outputs`` supported,
$0.10 / $0.20 per 1M prompt / completion tokens. ``usage.cost`` is the
provider-reported receipt. ``ev models verify`` re-reads the listing, so a
configured id that stops resolving fails loudly instead of drifting.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Sequence
from typing import Any

from app.config import settings
from app.contracts import ChatMessage, ChatResult
from app.gateway.providers import OpenAICompatibleProvider

logger = logging.getLogger("ev.gateway.brain")

_ALLOWED_EFFORTS = {"low", "medium", "high"}


class BrainUnavailable(RuntimeError):
    """The brain is unconfigured or returned an unusable response."""


class BrainEgressDenied(RuntimeError):
    """Remote chat egress is not permitted by the owner's configuration."""


_STRUCTURED_EMPTY_THRESHOLD = 3
_STRUCTURED_OUTAGE_COOLDOWN_SECONDS = 300.0


class _StructuredOutageBreaker:
    """Fast-fail for a dead structured-output adapter.

    After a few consecutive HTTP-200 empty-content schema calls, fail fast
    for a cooldown and let callers fall back to plain chat immediately; a
    lone probe per cooldown detects recovery.
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


def default_brain_model() -> str:
    """The configured brain model id (a setting, never a literal at call sites)."""

    return (settings.brain_model or "").strip()


class MuseSparkProvider(OpenAICompatibleProvider):
    """OpenAI-compatible OpenRouter provider for the Muse Spark brain."""

    name = "muse"
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
            default_model=(default_model or default_brain_model()),
            provider_name="muse",
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
            or getattr(settings, "brain_reasoning_effort", None)
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

    def _message_payload(self, message: ChatMessage) -> dict:
        # The listing declares text/image/video/file input only. Refuse audio
        # here, loudly, instead of letting the route answer HTTP 400.
        if any(part.kind == "audio" for part in message.media):
            raise BrainUnavailable(
                f"{self.default_model} does not accept audio input; "
                "transcribe speech before it reaches the brain"
            )
        return super()._message_payload(message)

    async def _authorize(self) -> None:
        """Fail closed on the remote-egress gate and a missing key.

        The revocable ``chat_egress`` consent record is checked too; when it is
        absent the call proceeds but logs a warning so the gap stays visible
        instead of silent.
        """

        from app.compliance.policy import remote_processing_allowed

        if not remote_processing_allowed("chat_egress"):
            raise BrainEgressDenied(
                "The brain is blocked: EV_ALLOW_REMOTE_CHAT is not enabled"
            )
        if not (self.api_key or "").strip():
            raise BrainUnavailable(
                "The brain is unavailable: EV_OPENROUTER_API_KEY is not set"
            )
        try:
            from app.db import SessionLocal
            from app.training.consent import active_consent

            async with SessionLocal() as session:
                consent = await active_consent(session, "chat_egress")
        except Exception:  # noqa: BLE001 - consent lookup must not break chat
            consent = None
        if consent is None:
            logger.warning(
                "chat_egress consent record is absent; remote brain calls are "
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
            raise BrainUnavailable(
                "Brain structured output is in outage cooldown; use plain chat"
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
        raise BrainUnavailable("The brain did not return the requested structured object")
