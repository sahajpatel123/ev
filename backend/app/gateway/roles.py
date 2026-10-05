"""Central model-role resolver — two-model workspace.

One place answers "which brain for this job":

- ``resolve_text_brain()``  -> MiMo (``xiaomi/mimo-v2.6-flash`` on OpenRouter).
- ``resolve_code_brain()``  -> MiMo (same single non-speech brain).
- ``resolve_voice_mouth()`` -> ``gemini-3.8-live-extended-thinking`` (speech coprocessor).

No silent fallback: when the owning role cannot serve (missing key,
consent, egress), the resolver raises the provider's own fail-closed error
instead of substituting another model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BrainSelection:
    """One explainable role assignment."""

    role: str  # text | code | mouth
    provider: str
    model: str
    reason: str


@dataclass(frozen=True)
class DecisionQuestion:
    """One finite choice question for ``decide_via_role``."""

    type: str  # "choice"
    instructions: str
    criteria: Any = None  # Mapping[str, str] | Sequence[str] | None


@dataclass(frozen=True)
class DecisionAnswer:
    """One validated choice answer."""

    type: str  # "choice"
    choice: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "choice": self.choice}


def _mimo_selection(role: str) -> BrainSelection:
    from app.config import settings

    model = (getattr(settings, "mimo_model", None) or "xiaomi/mimo-v2.6-flash").strip()
    return BrainSelection(
        role=role,
        provider="mimo",
        model=model,
        reason="mimo_single_brain",
    )


def resolve_text_brain() -> BrainSelection:
    """Non-coding decision + task-control brain: always MiMo."""

    return _mimo_selection("text")


def resolve_code_brain() -> BrainSelection:
    """Coding brain: always MiMo (same single non-speech brain)."""

    return _mimo_selection("code")


def resolve_voice_mouth() -> BrainSelection:
    """Speech coprocessor. Mouth only: VAD/ASR/TTS, never tools or decisions."""

    from app.config import settings

    model = (settings.gemini_live_model or "gemini-3.8-live-extended-thinking").strip()
    return BrainSelection(
        role="mouth",
        provider="gemini-live",
        model=model,
        reason="gemini_is_speech_coprocessor",
    )


def require_text_provider():
    """Instantiate the owning text-role provider: ``EV_CHAT_PROVIDER``.

    The registry holds ``echo`` / ``mock`` (offline doubles) and ``mimo``
    (the single cloud brain); anything else raises instead of substituting.
    """

    from app.gateway.providers import get_chat_provider

    return get_chat_provider()


def require_code_provider():
    """Instantiate the owning code-role provider (same single brain)."""

    from app.gateway.providers import get_chat_provider

    return get_chat_provider()


def text_brain_active() -> bool:
    """True when the MiMo text brain owns reasoning (every topology).

    Ownership, not servability: use text_role_available() for the key-gated
    can-serve pre-check.
    """

    return resolve_text_brain().provider == "mimo"


def text_role_available() -> bool:
    """Cheap, network-free pre-check that the owning text brain can serve.

    Only the keyed MiMo brain reports available. Offline doubles (echo/mock)
    never do: callers fall back to deterministic doubles instead of treating
    canned prose as generative output. The runtime egress gate is still
    enforced per request.
    """

    from app.config import settings

    key = (getattr(settings, "openrouter_api_key", None) or "").strip()
    return bool(getattr(settings, "mimo_enabled", True) and key)


def text_role_model() -> str:
    """Model id for display/health surfaces; never a request override."""

    return resolve_text_brain().model


def note_text_call(*, usage: dict | None = None) -> None:
    """Record the call in the owning brain's counters.

    Kept as a hook for the model-call audit; per-provider token counters
    were provider-specific and are gone with that lane.
    """

    del usage


async def chat_via_role(
    messages,
    *,
    model: str | None = None,
    temperature: float = 0.7,
    reasoning_effort: str | None = None,
):
    """One chat turn on the owning non-coding brain (MiMo)."""

    provider = require_text_provider()
    if reasoning_effort is not None and hasattr(provider, "reasoning_effort"):
        provider.reasoning_effort = reasoning_effort
    return await provider.chat(messages, model=model, temperature=temperature)


async def chat_structured_via_role(
    messages,
    *,
    schema: dict,
    schema_name: str = "turn_intent",
    model: str | None = None,
    reasoning_effort: str | None = None,
):
    """Structured JSON through the owning generative text-role brain (MiMo)."""

    provider = require_text_provider()
    if reasoning_effort is not None and hasattr(provider, "reasoning_effort"):
        provider.reasoning_effort = reasoning_effort
    if hasattr(provider, "chat_structured"):
        return await provider.chat_structured(
            messages, schema=schema, schema_name=schema_name, model=model
        )
    import logging

    logging.getLogger("ev.gateway.roles").warning(
        "provider %s lacks chat_structured; returning prose for schema %s",
        getattr(provider, "name", type(provider).__name__),
        schema_name,
    )
    return await provider.chat(messages, model=model)


def _choice_options(question: DecisionQuestion) -> list[str]:
    criteria = getattr(question, "criteria", None)
    if isinstance(criteria, dict):
        raw = list(criteria.keys())
    elif isinstance(criteria, (list, tuple)):
        raw = list(criteria)
    else:
        raw = []
    seen: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text and text not in seen:
            seen.append(text)
    return seen


async def decide_via_role(
    state,
    questions,
    *,
    session=None,
    actor: str = "system",
):
    """Run finite choice questions on MiMo structured output and audit it.

    ``questions`` maps ids to ``DecisionQuestion`` (or any object with
    ``instructions`` / ``criteria``). Returns a ``GatewayCall`` whose
    ``decision_answers`` carry validated choices; anything outside the
    declared options is an ``error`` call, never a guess. Provider failures
    raise (``MimoUnavailable`` / ``MimoEgressDenied``) so callers degrade
    explicitly; every completed call is audit-written.
    """

    import time
    from uuid import uuid4

    from app.contracts import ChatMessage, RequestEnvelope
    from app.gateway.service import GatewayCall

    items = list((questions or {}).items())
    if not items:
        raise ValueError("decide_via_role requires at least one question")
    prompt_lines = [json.dumps(state, default=str)[:4000], ""]
    properties: dict[str, Any] = {}
    allowed: dict[str, list[str]] = {}
    for qid, question in items:
        options = _choice_options(question)
        if len(options) < 2:
            raise ValueError(f"decision question {qid!r} needs at least two choices")
        instructions = str(getattr(question, "instructions", "") or "").strip()
        if not instructions:
            raise ValueError(f"decision question {qid!r} needs instructions")
        allowed[qid] = options
        properties[qid] = {"type": "string", "enum": options}
        prompt_lines.append(f"{qid}: {instructions} Choose exactly one: {', '.join(options)}.")
    schema = {"type": "object", "properties": properties, "required": sorted(properties)}
    provider = require_text_provider()
    messages = [ChatMessage(role="user", content="\n".join(prompt_lines))]
    started = time.perf_counter()
    if hasattr(provider, "chat_structured"):
        result = await provider.chat_structured(
            messages, schema=schema, schema_name="role_decision"
        )
    else:
        # Offline doubles speak prose only; validation below still rejects
        # anything outside the declared options.
        result = await provider.chat(messages)
    try:
        parsed = json.loads((result.text or "").strip() or "{}")
    except ValueError:
        parsed = {}
    answers: dict[str, DecisionAnswer] = {}
    issues: list[str] = []
    if not isinstance(parsed, dict):
        parsed = {}
    for qid, options in allowed.items():
        choice = parsed.get(qid)
        if isinstance(choice, str) and choice.strip() in options:
            answers[qid] = DecisionAnswer(type="choice", choice=choice.strip())
        else:
            issues.append(f"{qid}: no valid owner-defined choice")
    envelope = RequestEnvelope(
        request_id=str(uuid4()),
        strategy={"kind": "role_decision"},
        metadata={"decision_surface": "gateway_roles", "actor": actor},
    )
    call = GatewayCall(
        provider=getattr(provider, "name", "mimo"),
        request_id=envelope.request_id,
        envelope=envelope,
        result=result,
        decision_answers=answers,
        latency_ms=round((time.perf_counter() - started) * 1000, 1),
        status="error" if issues else "ok",
        error="; ".join(issues) if issues else None,
    )
    await _audit_decision(call, session=session, actor=actor)
    return call


async def _audit_decision(call, *, session, actor: str) -> None:
    from app.services.model_call import log_model_call

    try:
        if session is not None:
            await log_model_call(session, call=call, actor=actor)
            return
        from app.db import SessionLocal

        async with SessionLocal() as own:
            await log_model_call(own, call=call, actor=actor)
            await own.commit()
    except Exception as exc:  # noqa: BLE001 - audit failure is visible, never fatal
        import logging

        logging.getLogger("ev.gateway.roles").warning(
            "decision audit write failed: %s", exc
        )
        call.degraded = True
        call.degradation = {"kind": "audit_write_failed", "error": type(exc).__name__}


def answer_choice(call, question_id: str) -> str | None:
    """Return a validated choice answer, or ``None`` when it is absent/invalid."""

    answer = (getattr(call, "decision_answers", None) or {}).get(question_id)
    if answer is None or getattr(answer, "type", None) != "choice":
        return None
    choice = getattr(answer, "choice", None)
    return choice if isinstance(choice, str) else None
