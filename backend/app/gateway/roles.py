"""Central model-role resolver (Agent 10 CORTEX).

One place answers "which brain for this job":

- ``resolve_text_brain()``  -> JEV (``openrouter``) in jev_kernel, else Spark/legacy.
- ``resolve_code_brain()``  -> ALWAYS ``meta_muse_spark`` (``EV_CODE_MODEL`` lane).
- ``resolve_voice_mouth()`` -> ``gpt-realtime-2.1-mini`` (speech coprocessor, no tools).

No silent fallback: when the owning role cannot serve (missing opt-in, key,
consent, egress), the resolver raises the provider's own fail-closed error
instead of substituting another model.
"""

from __future__ import annotations

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
class JevDecision:
    """One bounded choice and the gateway call that produced it."""

    choice: str | None
    validation: str
    issues: tuple[str, ...]
    call: Any


def _jev_selection(role: str) -> BrainSelection:
    from app.config import settings

    model = (getattr(settings, "jev_model", None) or "typesafe/jev-1.13").strip()
    return BrainSelection(
        role=role,
        provider="openrouter",
        model=model,
        reason="jev_kernel_text_role",
    )


def _spark_code_selection() -> BrainSelection:
    from app.gateway.muse import muse_spark_model

    return BrainSelection(
        role="code",
        provider="meta_muse_spark",
        model=muse_spark_model(),
        reason="code_lane_is_always_spark",
    )


def _mimo_selection(role: str) -> BrainSelection:
    from app.config import settings

    model = (getattr(settings, "mimo_model", None) or "xiaomi/mimo-v2.6-flash").strip()
    return BrainSelection(
        role=role,
        provider="mimo",
        model=model,
        reason="mimo_single_brain",
    )


def _mimo_owns_text() -> bool:
    """MiMo owns every non-speech role in both single-brain topologies.

    ``mimo_kernel`` gives MiMo the mouth too. ``realtime_delegate`` gives the
    mouth to Realtime Mini but keeps text, code, structured output, memory,
    and background work on MiMo — so a delegate process must never fall
    through to the removed Spark lane just because speech moved.
    """

    from app.cognitive.mode import mimo_kernel_active, realtime_delegate_active

    return mimo_kernel_active() or realtime_delegate_active()


def resolve_text_brain() -> BrainSelection:
    """Non-coding decision + task-control brain.

    JEV owns this role in jev_kernel. Otherwise the configured intelligence
    provider (Spark single-brain or legacy split) answers, exactly as
    ``configured_intelligence_provider()`` already resolves.
    """

    from app.gateway.muse import configured_intelligence_provider, jev_kernel_active

    if _mimo_owns_text():
        return _mimo_selection("text")
    if jev_kernel_active():
        return _jev_selection("text")
    from app.config import settings

    name = (configured_intelligence_provider() or settings.chat_provider or "echo").strip()
    from app.gateway.muse import muse_spark_model

    if name.lower() in {"meta_muse_spark", "muse", "muse_spark"}:
        return BrainSelection(
            role="text", provider=name, model=muse_spark_model(), reason="muse_single_brain"
        )
    return BrainSelection(role="text", provider=name, model=name, reason="configured_provider")


def resolve_code_brain() -> BrainSelection:
    """Coding brain. Independent of the text role by owner order.

    ``EV_CODE_MODEL=muse-spark-1.3-contributor`` keeps code on Spark even
    when JEV owns every other decision. This resolver never returns JEV.
    """

    if _mimo_owns_text():
        return _mimo_selection("code")
    return _spark_code_selection()


def resolve_voice_mouth() -> BrainSelection:
    """Speech coprocessor. Mouth only: VAD/ASR/TTS, never tools or decisions."""

    from app.config import settings

    model = (settings.openai_realtime_model or "gpt-realtime-2.1-mini").strip()
    return BrainSelection(
        role="mouth",
        provider="openai-realtime",
        model=model,
        reason="mini_is_speech_coprocessor",
    )


def require_text_provider():
    """Instantiate the owning text-role provider, failing closed (no substitute)."""

    from app.gateway.providers import PROVIDER_REGISTRY, UnknownProviderError

    selection = resolve_text_brain()
    if selection.provider == "openrouter":
        raise UnknownProviderError(
            "the text role is JEV decisions-only and cannot generate prose; "
            "use require_decision_provider() with ModelGateway.decide()"
        )
    factory = PROVIDER_REGISTRY.get(selection.provider)
    if factory is None:
        raise UnknownProviderError(
            f"text role resolved to unknown provider {selection.provider!r}; refusing to substitute"
        )
    return factory()


def require_decision_provider():
    """Return JEV only when it owns the configured decision role."""

    from app.gateway.muse import jev_kernel_active
    from app.gateway.providers import decision_provider_from_selection

    if not jev_kernel_active():
        raise RuntimeError("JEV does not own the active text role")
    return decision_provider_from_selection(resolve_text_brain())


def require_code_provider():
    """Instantiate the Spark code-lane provider, failing closed (never JEV)."""

    from app.gateway.muse_spark import muse_spark_provider

    return muse_spark_provider()


def text_brain_active() -> bool:
    """True when a non-coding text brain owns reasoning (Muse, JEV or MiMo)."""

    from app.gateway.muse import jev_kernel_active, muse_brain_active

    return muse_brain_active() or jev_kernel_active() or _mimo_owns_text()


def text_role_available() -> bool:
    """Cheap, network-free pre-check that the owning text brain can serve.

    Under ``mimo_kernel`` this needs the opt-in and the OpenRouter key; under
    ``jev_kernel`` the same. The runtime egress gate is still enforced per
    request. It never reports the other brain as available to keep the role
    assignment honest.
    """

    from app.gateway.muse import jev_kernel_active, muse_spark_key_loaded

    if _mimo_owns_text():
        from app.config import settings

        key = (getattr(settings, "openrouter_api_key", None) or "").strip()
        return bool(getattr(settings, "mimo_enabled", True) and key)
    if jev_kernel_active():
        from app.config import settings

        key = (getattr(settings, "openrouter_api_key", None) or "").strip()
        return bool(getattr(settings, "jev_enabled", False) and key)
    return muse_spark_key_loaded()


def text_role_model() -> str:
    """Model id for display/health surfaces; never a request override."""

    return resolve_text_brain().model


def note_text_call(*, usage: dict | None = None) -> None:
    """Record the call in the owning brain's counters (Spark only)."""

    from app.gateway.muse import jev_kernel_active

    if jev_kernel_active() or _mimo_owns_text():
        return
    try:
        from app.gateway.muse import note_spark_call

        note_spark_call(usage=usage)
    except Exception:  # noqa: BLE001 - telemetry must never break a call
        pass


async def chat_via_role(
    messages,
    *,
    model: str | None = None,
    temperature: float = 0.7,
    reasoning_effort: str | None = None,
):
    """One chat turn on the owning non-coding brain.

    JEV has no sampling parameters and refuses model overrides, so ``model``
    is a Spark-only hint. MiMo and Spark honor ``reasoning_effort``.
    """

    from app.gateway.muse import jev_kernel_active

    if _mimo_owns_text():
        provider = require_text_provider()
        if reasoning_effort is not None:
            provider.reasoning_effort = reasoning_effort
        return await provider.chat(messages, temperature=temperature)
    if jev_kernel_active():
        provider = require_text_provider()
        return await provider.chat(messages, temperature=temperature)
    from app.gateway.muse_spark import muse_spark_provider

    provider = muse_spark_provider()
    if reasoning_effort is not None:
        return await provider.chat(
            messages,
            model=model,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
        )
    return await provider.chat(messages, model=model, temperature=temperature)


async def chat_structured_via_role(
    messages,
    *,
    schema: dict,
    schema_name: str = "turn_intent",
    model: str | None = None,
    reasoning_effort: str | None = None,
):
    """Structured JSON through the owning generative text-role brain.

    MiMo returns JSON-schema output; JEV cannot generate arbitrary JSON or
    prose (decision callers use ``ModelGateway.decide()``).
    """

    from app.gateway.muse import jev_kernel_active

    if _mimo_owns_text():
        provider = require_text_provider()
        if reasoning_effort is not None:
            provider.reasoning_effort = reasoning_effort
        return await provider.chat_structured(
            messages, schema=schema, schema_name=schema_name
        )
    if jev_kernel_active():
        from app.gateway.openrouter_jev import OpenRouterJevUnavailable

        raise OpenRouterJevUnavailable(
            f"JEV cannot generate structured output for {schema_name!r}; "
            "define a JevQuestion and call ModelGateway.decide()"
        )
    from app.gateway.muse import muse_spark_model
    from app.gateway.muse_spark import muse_spark_provider

    provider = muse_spark_provider()
    hint = model or muse_spark_model()
    if reasoning_effort is not None:
        return await provider.chat_structured(
            messages,
            schema=schema,
            schema_name=schema_name,
            model=hint,
            reasoning_effort=reasoning_effort,
        )
    return await provider.chat_structured(
        messages, schema=schema, schema_name=schema_name, model=hint
    )


async def decide_via_role(
    state,
    questions,
    *,
    session=None,
    actor: str = "system",
):
    """Run one typed decision on the owning decision provider and audit it.

    Only valid when JEV owns the text role; callers branch on
    ``jev_kernel_active()`` first. Every decision is written to the model-call
    audit even when the caller has no session of its own; an audit-write
    failure is attached to the returned call as a visible degradation.
    """

    from uuid import uuid4

    from app.contracts import RequestEnvelope
    from app.gateway.service import ModelGateway

    provider = require_decision_provider()
    gateway = ModelGateway(provider)
    envelope = RequestEnvelope(
        request_id=str(uuid4()),
        strategy={"kind": "role_decision", "role": resolve_text_brain().role},
    )
    call = await gateway.decide(state, questions, envelope=envelope)
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


async def choose_with_jev(
    gateway,
    messages,
    *,
    envelope,
    session,
    actor: str,
    question_id: str,
    choices: tuple[str, ...] | list[str],
    instructions: str,
) -> JevDecision:
    """Ask one finite choice through the typed gateway API."""

    from app.contracts import ChatMessage
    from app.gateway.openrouter_jev import JevQuestion, OpenRouterJevUnavailable

    if getattr(getattr(gateway, "provider", None), "name", "") != "openrouter":
        raise ValueError("choose_with_jev requires the OpenRouter JEV decision provider")
    allowed = tuple(dict.fromkeys(str(choice).strip() for choice in choices if str(choice).strip()))
    if not allowed:
        raise ValueError("JEV choice options must be non-empty")

    state_messages: list[dict[str, str]] = []
    for message in messages:
        if not isinstance(message, ChatMessage):
            raise TypeError("JEV messages must use the ChatMessage contract")
        content = message.content or ""
        derived: list[str] = []
        for part in message.media:
            if part.data_url:
                raise OpenRouterJevUnavailable(
                    "JEV accepts derived text only; raw media was refused"
                )
            if part.text:
                derived.append(part.text)
        if derived:
            content = "\n".join([content, *derived]).strip()
        state_messages.append({"role": message.role, "content": content})

    question = JevQuestion(
        type="choice",
        instructions=instructions,
        criteria={choice: choice.replace("_", " ") for choice in allowed},
    )
    call = await gateway.decide(
        {"messages": state_messages},
        {question_id: question},
        envelope=envelope,
    )
    from app.services.model_call import log_model_call

    await log_model_call(session, call=call, actor=actor)
    if call.status != "ok":
        return JevDecision(
            choice=None,
            validation=call.status,
            issues=((call.error or "JEV decision failed"),),
            call=call,
        )
    answer = (call.decision_answers or {}).get(question_id)
    if answer is None or answer.type != "choice" or answer.choice not in allowed:
        return JevDecision(
            choice=None,
            validation="rejected",
            issues=("JEV returned no valid owner-defined choice",),
            call=call,
        )
    return JevDecision(choice=answer.choice, validation="ok", issues=(), call=call)
