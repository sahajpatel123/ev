"""Supervisor verdict model via the OpenRouter Decisions API.

The decider (`perplexity/pplx-decider-v1-27b` by default) is a decision
model, not a chat model: it reads a `state` plus typed `questions` and
returns calibrated probabilities in one forward pass — `noul` (yes/no),
`choice` (options you define), `score` (ordered levels). There is no
generated text to parse, which is exactly why it supervises workers: the
answer shape is fixed by the request, not hoped for in prose.

It rides the same `EV_OPENROUTER_API_KEY` as MiMo; only the model id and
the endpoint (`/api/alpha/decisions`, not `/v1/chat/completions`) differ.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger("ev.gateway.decider")


class DeciderUnavailable(RuntimeError):
    """The decider model is unconfigured or returned an unusable response."""


class DeciderEgressDenied(RuntimeError):
    """Remote verdict calls are not permitted by the owner's configuration."""


@dataclass
class DeciderResult:
    """Typed answers from one Decisions API call."""

    answers: dict[str, Any]
    model: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)

    @property
    def cost_usd(self) -> float | None:
        cost = self.usage.get("cost")
        if isinstance(cost, bool) or not isinstance(cost, (int, float)):
            return None
        return float(cost)


class DeciderProvider:
    """OpenRouter Decisions API client for the supervisor verdict model."""

    name = "decider"

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        api_key: str | None = None,
        default_model: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.endpoint = (endpoint or settings.decider_endpoint).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.openrouter_api_key
        self.default_model = default_model or settings.decider_model
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

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

    def build_payload(
        self,
        state: dict[str, Any],
        questions: dict[str, Any],
        *,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Decisions envelope: model id, arbitrary state, named questions."""

        return {
            "model": model or self.default_model,
            "state": state,
            "questions": questions,
        }

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        timeout = float(getattr(settings, "decider_timeout_seconds", 30.0) or 30.0)
        async with httpx.AsyncClient(
            timeout=timeout, transport=self._transport
        ) as client:
            response = await client.post(
                self.endpoint, json=payload, headers=self._headers()
            )
        if response.status_code != 200:
            raise DeciderUnavailable(
                f"Decisions API HTTP {response.status_code}: {response.text[:200]}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise DeciderUnavailable(
                "Decisions API returned a non-JSON response"
            ) from exc
        if not isinstance(body, dict):
            raise DeciderUnavailable("Decisions API response is not an object")
        if isinstance(body.get("error"), dict):
            detail = body["error"].get("message", body["error"])
            raise DeciderUnavailable(f"Decisions API error: {detail}")
        if not isinstance(body.get("answers"), dict):
            raise DeciderUnavailable("Decisions API response has no answers object")
        return body

    async def judge(
        self,
        state: dict[str, Any],
        questions: dict[str, Any],
        *,
        model: str | None = None,
    ) -> DeciderResult:
        """One typed verdict call; malformed answers are never trusted."""

        await self._authorize()
        if not isinstance(questions, dict) or not questions:
            raise DeciderUnavailable("Decider called without questions")
        try:
            body = await self._post(
                self.build_payload(state, questions, model=model)
            )
        except httpx.HTTPError as exc:
            raise DeciderUnavailable(
                f"Decisions API transport error: {exc}"
            ) from exc
        usage = body.get("usage")
        result = DeciderResult(
            answers=body["answers"],
            model=body.get("model"),
            usage=usage if isinstance(usage, dict) else {},
        )
        if result.cost_usd is not None:
            logger.debug(
                "decider verdict model=%s cost_usd=%.6f",
                result.model,
                result.cost_usd,
            )
        return result


def decider_available() -> bool:
    """Cheap, network-free pre-check that the verdict model can serve.

    Same key as the MiMo text brain: without it the supervisor falls back to
    the deterministic local verdict instead of treating a guess as a judgment.
    """

    key = (getattr(settings, "openrouter_api_key", None) or "").strip()
    model = (getattr(settings, "decider_model", None) or "").strip()
    return bool(key and model)
