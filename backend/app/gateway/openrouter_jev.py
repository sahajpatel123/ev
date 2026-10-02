"""Typed OpenRouter Decisions API adapter for TypeSafe JEV 1.13.

JEV accepts a JSON state and typed ``choice``, ``score`` or ``noul`` questions.
It returns typed answers and does not generate prose, chat messages or tool
calls. Requests use OpenRouter's API-key authenticated Decisions endpoint.

Live-verified 2026-10-01 with the owner's key: ``POST /api/alpha/decisions``
returns ``200`` with typed answers, probabilities, confidence, usage and a
cost receipt; the same model is rejected (HTTP 400) on
``/api/v1/chat/completions`` ("is a decisions model and cannot be used with
the chat/completions endpoint").
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx

from app.config import settings
from app.gateway.costs import reported_openrouter_cost  # noqa: F401 - re-export
from app.gateway.reliability import (
    CIRCUIT_BREAKERS,
    CircuitOpenError,
    http_timeout,
    is_transient,
    max_attempts,
    wait_for_retry,
)
from app.security.boundary import MODEL_FORBIDDEN_MARKERS, SECRET_REDACTION, redact_secrets

JevQuestionType = Literal["choice", "score", "noul"]
_QUESTION_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_DATA_URI_RE = re.compile(r"\bdata:[^,\s]+,", re.IGNORECASE)
_RAW_MEDIA_KEYS = frozenset(
    {
        "attachment", "attachments", "audio", "audio_data", "audio_payload",
        "base64", "binary", "blob", "bytes", "data_url", "frame", "frames",
        "image", "image_data", "image_payload", "images", "media", "media_parts",
        "pixels", "raw_bytes", "screenshot", "video", "video_data",
        "video_payload", "waveform",
    }
)
_RAW_MEDIA_TYPES = frozenset({"audio", "frame", "image", "media", "video"})
_SECRET_KEYS = frozenset(
    {
        "access_token", "accesstoken", "api_key", "apikey", "authorization",
        "credential", "credentials", "password", "passwd", "private_key",
        "privatekey", "refresh_token", "refreshtoken", "secret", "token",
    }
)


class OpenRouterJevError(RuntimeError):
    """Base error for JEV Decisions API failures."""


class OpenRouterJevDisabled(OpenRouterJevError):
    """Raised when JEV is selected without its explicit opt-in flag."""


class OpenRouterJevUnavailable(OpenRouterJevError):
    """The provider is unconfigured or returned an invalid response."""


class OpenRouterEgressDenied(OpenRouterJevError):
    """Remote processing or active owner consent does not permit egress."""


@dataclass(frozen=True)
class JevQuestion:
    """One typed Decisions API question."""

    type: JevQuestionType
    instructions: str
    criteria: Mapping[str, str] | Sequence[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return normalize_question(self)


@dataclass(frozen=True)
class JevAnswer:
    """A validated typed answer returned by JEV."""

    type: JevQuestionType
    choice: str | None = None
    score: float | None = None
    noul: float | None = None
    probabilities: dict[str, float] = field(default_factory=dict)
    confidence: float | None = None
    legend: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        answer: dict[str, Any] = {"type": self.type}
        if self.choice is not None:
            answer["choice"] = self.choice
        if self.score is not None:
            answer["score"] = self.score
        if self.noul is not None:
            answer["noul"] = self.noul
        if self.probabilities:
            answer["probabilities"] = dict(self.probabilities)
        if self.confidence is not None:
            answer["confidence"] = self.confidence
        if self.legend:
            answer["legend"] = dict(self.legend)
        return answer


@dataclass(frozen=True)
class JevDecisionResult:
    """Validated answers plus provider receipt and timing metadata."""

    model: str
    answers: dict[str, JevAnswer]
    usage: dict[str, Any]
    response_id: str | None
    latency_ms: float
    payload_sha256: str | None = None


class DecisionProvider(Protocol):
    """Provider contract for typed decisions rather than generated text."""

    name: str

    def decision_payload(
        self,
        state: object,
        questions: Mapping[str, JevQuestion | Mapping[str, Any]],
        *,
        model: str | None = None,
    ) -> dict[str, Any]: ...

    async def decide(
        self,
        state: object,
        questions: Mapping[str, JevQuestion | Mapping[str, Any]],
        *,
        model: str | None = None,
    ) -> JevDecisionResult: ...


def _safe_text(text: str, *, path: str) -> str:
    if _DATA_URI_RE.search(text):
        raise OpenRouterJevUnavailable("JEV accepts sanitized text only; data URLs were refused")
    lowered = text.lower()
    if any(marker in lowered for marker in MODEL_FORBIDDEN_MARKERS):
        raise OpenRouterJevUnavailable(f"JEV state contains never-send content at {path}")
    return redact_secrets(text)


def _safe_json(value: Any, path: str = "state") -> Any:
    """Sanitize JSON-compatible state and reject binary or raw media objects."""

    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        return _safe_text(value, path=path)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise OpenRouterJevUnavailable(f"JEV state contains a non-finite number at {path}")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise OpenRouterJevUnavailable(f"JEV state has a non-string key at {path}")
        keys = {key.lower().replace("-", "_"): key for key in value}
        raw_kind = value.get("type", value.get("kind", ""))
        if isinstance(raw_kind, str) and raw_kind.strip().lower() in _RAW_MEDIA_TYPES:
            raise OpenRouterJevUnavailable("JEV accepts derived text only; media objects were refused")
        for key in ("mime_type", "content_type", "media_type"):
            actual = keys.get(key)
            media_type = value.get(actual) if actual is not None else None
            if isinstance(media_type, str) and media_type.lower().startswith(
                ("image/", "audio/", "video/")
            ):
                raise OpenRouterJevUnavailable("JEV accepts derived text only; media objects were refused")
        if "data" in keys and any(key in keys for key in ("mime_type", "content_type", "media_type")):
            raise OpenRouterJevUnavailable("JEV accepts derived text only; media objects were refused")
        clean: dict[str, Any] = {}
        for original_key, child in value.items():
            normalized_key = original_key.lower().replace("-", "_")
            if normalized_key in _RAW_MEDIA_KEYS:
                raise OpenRouterJevUnavailable("JEV accepts derived text only; media objects were refused")
            key = _safe_text(original_key, path=f"{path}.<key>")
            if key in clean:
                raise OpenRouterJevUnavailable(f"JEV state has duplicate keys after redaction at {path}")
            clean[key] = SECRET_REDACTION if normalized_key in _SECRET_KEYS else _safe_json(
                child, f"{path}.{key}"
            )
        return clean
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_safe_json(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise OpenRouterJevUnavailable("JEV accepts derived text only; binary media was refused")
    raise OpenRouterJevUnavailable(f"JEV state is not JSON-compatible at {path}")


def normalize_question(question: JevQuestion | Mapping[str, Any]) -> dict[str, Any]:
    """Validate and sanitize one Decisions API question."""

    if isinstance(question, JevQuestion):
        raw = {"type": question.type, "instructions": question.instructions, "criteria": question.criteria}
    elif isinstance(question, Mapping):
        raw = dict(question)
    else:
        raise OpenRouterJevUnavailable("JEV question must be a JevQuestion or object")

    kind = raw.get("type")
    instructions = raw.get("instructions")
    if kind not in {"choice", "score", "noul"}:
        raise OpenRouterJevUnavailable("JEV question type must be choice, score or noul")
    if not isinstance(instructions, str) or not instructions.strip() or len(instructions) > 4000:
        raise OpenRouterJevUnavailable("JEV question instructions must contain 1 to 4000 characters")
    result: dict[str, Any] = {
        "type": kind,
        "instructions": _safe_text(instructions.strip(), path="question.instructions"),
    }
    criteria = raw.get("criteria")
    if kind == "choice":
        if not isinstance(criteria, Mapping) or not 2 <= len(criteria) <= 64:
            raise OpenRouterJevUnavailable("JEV choice criteria must define 2 to 64 options")
        options: dict[str, str] = {}
        for option, description in criteria.items():
            if (
                not isinstance(option, str)
                or not option.strip()
                or len(option) > 96
                or not isinstance(description, str)
                or not description.strip()
                or len(description) > 1000
            ):
                raise OpenRouterJevUnavailable("JEV choice criteria must map labels to descriptions")
            label = _safe_text(option.strip(), path="question.criteria.<option>")
            if not label or label in options:
                raise OpenRouterJevUnavailable("JEV choice labels are empty or collide after redaction")
            options[label] = _safe_text(description.strip(), path=f"question.criteria.{label}")
        result["criteria"] = options
    elif kind == "score":
        if not isinstance(criteria, Sequence) or isinstance(criteria, (str, bytes)):
            raise OpenRouterJevUnavailable("JEV score criteria must be an ordered list")
        if not 2 <= len(criteria) <= 10 or any(
            not isinstance(level, str) or not level.strip() or len(level) > 1000
            for level in criteria
        ):
            raise OpenRouterJevUnavailable("JEV score criteria must contain 2 to 10 level descriptions")
        result["criteria"] = [
            _safe_text(level.strip(), path=f"question.criteria[{index}]")
            for index, level in enumerate(criteria)
        ]
    elif criteria is not None:
        if not isinstance(criteria, Mapping) or set(criteria) != {"true", "false"}:
            raise OpenRouterJevUnavailable("JEV Noul criteria must define exactly true and false")
        if any(not isinstance(item, str) or not item.strip() or len(item) > 1000 for item in criteria.values()):
            raise OpenRouterJevUnavailable("JEV Noul criteria descriptions must be non-empty text")
        result["criteria"] = {
            key: _safe_text(criteria[key].strip(), path=f"question.criteria.{key}")
            for key in ("true", "false")
        }
    return result


def normalize_questions(
    questions: Mapping[str, JevQuestion | Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    if not isinstance(questions, Mapping) or not 1 <= len(questions) <= 32:
        raise OpenRouterJevUnavailable("JEV requires 1 to 32 typed questions")
    normalized: dict[str, dict[str, Any]] = {}
    for question_id, question in questions.items():
        if not isinstance(question_id, str) or not _QUESTION_ID_RE.fullmatch(question_id):
            raise OpenRouterJevUnavailable("JEV question ids must be short ASCII identifiers")
        if redact_secrets(question_id) != question_id:
            raise OpenRouterJevUnavailable("JEV question ids cannot contain credential-like text")
        normalized[question_id] = normalize_question(question)
    return normalized


def _finite_number(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _probabilities(raw: Any, expected: set[str], question_id: str) -> dict[str, float]:
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise OpenRouterJevUnavailable(f"JEV answer {question_id!r} has invalid probability keys")
    result: dict[str, float] = {}
    for key, value in raw.items():
        number = _finite_number(value)
        if number is None or not 0 <= number <= 1:
            raise OpenRouterJevUnavailable(f"JEV answer {question_id!r} has an invalid probability")
        result[str(key)] = number
    if abs(sum(result.values()) - 1.0) > 0.03:
        raise OpenRouterJevUnavailable(f"JEV answer {question_id!r} probabilities do not sum to one")
    return result


def _unit_interval(value: Any, field_name: str, question_id: str) -> float:
    number = _finite_number(value)
    if number is None or not 0 <= number <= 1:
        raise OpenRouterJevUnavailable(f"JEV answer {question_id!r} has an invalid {field_name}")
    return number


def validate_answer(question_id: str, question: Mapping[str, Any], raw: Any) -> JevAnswer:
    """Validate one answer against its exact requested type and criteria.

    ``probabilities``/``confidence``/``legend`` are optional: the live API
    returns them, but EV never fabricates them when a provider omits them.
    When present they are validated exactly.
    """

    if not isinstance(raw, Mapping) or raw.get("type") != question.get("type"):
        raise OpenRouterJevUnavailable(f"JEV answer {question_id!r} has the wrong type")
    kind = question["type"]
    if kind == "choice":
        if not set(raw) <= {"type", "choice", "probabilities", "confidence"}:
            raise OpenRouterJevUnavailable(f"JEV answer {question_id!r} has unexpected fields")
        criteria = question["criteria"]
        choice = raw.get("choice")
        if not isinstance(choice, str) or choice not in criteria:
            raise OpenRouterJevUnavailable(f"JEV answer {question_id!r} is outside the requested choices")
        probabilities = raw.get("probabilities")
        confidence = raw.get("confidence")
        return JevAnswer(
            type="choice",
            choice=choice,
            probabilities=(
                _probabilities(probabilities, set(criteria), question_id)
                if probabilities is not None
                else {}
            ),
            confidence=(
                _unit_interval(confidence, "confidence", question_id)
                if confidence is not None
                else None
            ),
        )
    if kind == "score":
        if not set(raw) <= {"type", "score", "legend", "probabilities", "confidence"}:
            raise OpenRouterJevUnavailable(f"JEV answer {question_id!r} has unexpected fields")
        levels = question["criteria"]
        score = _finite_number(raw.get("score"))
        if score is None or not 0 <= score <= len(levels) - 1:
            raise OpenRouterJevUnavailable(f"JEV answer {question_id!r} has an invalid score")
        legend = {str(index): level for index, level in enumerate(levels)}
        if raw.get("legend") is not None and raw.get("legend") != legend:
            raise OpenRouterJevUnavailable(f"JEV answer {question_id!r} has an invalid score legend")
        probabilities = raw.get("probabilities")
        confidence = raw.get("confidence")
        return JevAnswer(
            type="score",
            score=score,
            probabilities=(
                _probabilities(probabilities, set(legend), question_id)
                if probabilities is not None
                else {}
            ),
            confidence=(
                _unit_interval(confidence, "confidence", question_id)
                if confidence is not None
                else None
            ),
            legend=legend,
        )
    if kind == "noul":
        if not set(raw) <= {"type", "noul"}:
            raise OpenRouterJevUnavailable(f"JEV answer {question_id!r} has unexpected fields")
        return JevAnswer(type="noul", noul=_unit_interval(raw.get("noul"), "noul", question_id))
    raise OpenRouterJevUnavailable(f"JEV answer {question_id!r} has an unsupported type")


def normalize_usage(raw: Any) -> dict[str, Any]:
    """Preserve the provider receipt and add existing gateway token aliases."""

    if not isinstance(raw, Mapping):
        return {"usage_missing": True}
    usage = dict(raw)
    for key in ("input_tokens", "output_tokens", "prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        if key in usage and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            usage.pop(key, None)
    if "input_tokens" in usage:
        usage["prompt_tokens"] = usage["input_tokens"]
    if "output_tokens" in usage:
        usage["completion_tokens"] = usage["output_tokens"]
    cost = _finite_number(usage.get("cost"))
    if cost is not None and cost >= 0:
        usage["cost"] = cost
        usage["cost_source"] = "openrouter_reported"
    else:
        usage.pop("cost", None)
        usage.pop("cost_source", None)
    if not any(key in usage for key in ("prompt_tokens", "completion_tokens", "cost")):
        usage["usage_missing"] = True
    else:
        usage.pop("usage_missing", None)
    return usage


class OpenRouterJevProvider:
    """API-key authenticated typed JEV Decisions API provider."""

    name = "openrouter"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        default_model: str | None = None,
    ) -> None:
        self.base_url = (base_url or settings.openrouter_base_url).rstrip("/")
        self.api_key = (api_key if api_key is not None else settings.openrouter_api_key) or ""
        self.default_model = (default_model or settings.jev_model).strip()

    def _decisions_url(self) -> str:
        """Validate the configured origin and return the fixed Decisions URL."""

        try:
            parsed = urlsplit(self.base_url)
            port = parsed.port
        except ValueError as exc:
            raise OpenRouterJevUnavailable("OpenRouter JEV destination is invalid") from exc
        if (
            parsed.scheme != "https"
            or parsed.hostname != "openrouter.ai"
            or port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/api", "/api/v1"}
        ):
            raise OpenRouterJevUnavailable(
                "OpenRouter JEV destination must be the trusted HTTPS OpenRouter API origin"
            )
        return "https://openrouter.ai/api/alpha/decisions"

    def decision_payload(
        self,
        state: object,
        questions: Mapping[str, JevQuestion | Mapping[str, Any]],
        *,
        model: str | None = None,
    ) -> dict[str, Any]:
        if model is not None and model != self.default_model:
            raise OpenRouterJevUnavailable(
                "OpenRouter JEV refuses per-request model overrides; use the configured JEV model"
            )
        if state is not None and not isinstance(
            state, (str, bool, int, float, Mapping, list, tuple)
        ):
            raise OpenRouterJevUnavailable(
                "JEV state must contain sanitized JSON-compatible text or data"
            )
        return {
            "model": self.default_model,
            "state": _safe_json(state),
            "questions": normalize_questions(questions),
        }

    def _headers(self) -> dict[str, str]:
        if not self.api_key.strip():
            raise OpenRouterJevUnavailable(
                "OpenRouter JEV is unavailable: EV_OPENROUTER_API_KEY is not set"
            )
        return {
            "Authorization": f"Bearer {self.api_key.strip()}",
            "Content-Type": "application/json",
        }

    @staticmethod
    async def _require_active_chat_egress_consent() -> None:
        """Check revocable owner consent before each authenticated request."""

        try:
            from app.db import SessionLocal
            from app.training.consent import active_consent

            async with SessionLocal() as session:
                consent = await active_consent(session, "chat_egress")
        except Exception as exc:  # noqa: BLE001 - remote egress must fail closed
            raise OpenRouterEgressDenied(
                "OpenRouter JEV is blocked: chat-egress consent could not be verified"
            ) from exc
        if consent is None:
            raise OpenRouterEgressDenied(
                "OpenRouter JEV is blocked: no active chat_egress consent record"
            )

    @staticmethod
    def _valid_model_id(value: Any, configured_model: str) -> bool:
        if value == configured_model:
            return True
        prefix = f"{configured_model}-"
        if not isinstance(value, str) or not value.startswith(prefix):
            return False
        suffix = value[len(prefix) :]
        try:
            if re.fullmatch(r"\d{8}", suffix):
                datetime.strptime(suffix, "%Y%m%d")
                return True
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", suffix):
                date.fromisoformat(suffix)
                return True
        except ValueError:
            return False
        return False

    async def decide(
        self,
        state: object,
        questions: Mapping[str, JevQuestion | Mapping[str, Any]],
        *,
        model: str | None = None,
    ) -> JevDecisionResult:
        from app.compliance.policy import remote_processing_allowed
        from app.gateway.muse import refuse_legacy_cloud_brain

        started = time.perf_counter()
        if not settings.jev_enabled:
            raise OpenRouterJevDisabled(
                "OpenRouter JEV is opt-in; set EV_JEV_ENABLED=true to enable it"
            )
        endpoint = self._decisions_url()
        if not remote_processing_allowed("chat_egress"):
            raise OpenRouterEgressDenied(
                "OpenRouter JEV is blocked: remote chat egress is not enabled"
            )
        refuse_legacy_cloud_brain(self.name)
        payload = self.decision_payload(state, questions, model=model)
        await self._require_active_chat_egress_consent()
        headers = self._headers()

        breaker = CIRCUIT_BREAKERS.get(self.name)
        if not breaker.allow_request():
            raise CircuitOpenError(self.name, breaker.retry_after_seconds())
        question_specs = payload["questions"]
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        payload_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        attempts = max_attempts()
        for attempt in range(attempts):
            try:
                async with httpx.AsyncClient(
                    timeout=http_timeout(), follow_redirects=False
                ) as client:
                    response = await client.post(endpoint, headers=headers, json=payload)
                    response.raise_for_status()
                body = response.json()
                if not isinstance(body, Mapping):
                    raise OpenRouterJevUnavailable(
                        "JEV returned a malformed Decisions API response"
                    )
                raw_answers = body.get("answers")
                if not isinstance(raw_answers, Mapping):
                    raise OpenRouterJevUnavailable(
                        "JEV returned a malformed Decisions API response"
                    )
                expected = set(question_specs)
                if set(raw_answers) != expected:
                    if expected - set(raw_answers):
                        raise OpenRouterJevUnavailable(
                            "JEV did not return exactly one answer for each requested question"
                        )
                    raise OpenRouterJevUnavailable("JEV answer had unexpected fields")
                answers = {
                    question_id: validate_answer(
                        question_id, question_specs[question_id], raw_answers[question_id]
                    )
                    for question_id in question_specs
                }
                raw_model = body.get("model")
                if not self._valid_model_id(raw_model, self.default_model):
                    raise OpenRouterJevUnavailable(
                        "JEV response model did not match the configured model"
                    )
                raw_id = body.get("id")
                if not isinstance(raw_id, str) or not raw_id.strip() or len(raw_id) > 256:
                    raise OpenRouterJevUnavailable("JEV returned an invalid response id")
                breaker.record_success()
                return JevDecisionResult(
                    model=str(raw_model),
                    answers=answers,
                    usage=normalize_usage(body.get("usage")),
                    response_id=raw_id.strip(),
                    latency_ms=round((time.perf_counter() - started) * 1000, 1),
                    payload_sha256=payload_sha256,
                )
            except OpenRouterJevError:
                breaker.record_failure()
                raise
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                transient = is_transient(exc, status)
                breaker.record_failure()
                if transient and attempt + 1 < attempts:
                    await wait_for_retry(attempt)
                    continue
                status_note = f", HTTP {status}" if status is not None else ""
                raise OpenRouterJevUnavailable(
                    f"OpenRouter JEV request failed ({type(exc).__name__}{status_note})"
                ) from exc
            except (ValueError, TypeError, KeyError) as exc:
                breaker.record_failure()
                raise OpenRouterJevUnavailable(
                    f"OpenRouter JEV returned an invalid response ({type(exc).__name__})"
                ) from exc
        raise OpenRouterJevUnavailable("OpenRouter JEV request exhausted retries")
