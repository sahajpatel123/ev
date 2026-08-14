"""Bounded, provider-independent tool loop for chat turns.

The gateway pre-validates model tool calls; this loop executes the valid ones
through the declarative dispatcher, feeds the results back as ``tool``
messages, and re-enters the provider until no calls remain or the round cap is
hit. Every gateway round and every dispatcher invocation is audited, so the
model can interrogate memory without ever invoking arbitrary functionality.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import ChatMessage, ToolSpec
from app.ev import tools as ev_tools
from app.ev.briefing import WRITE_TOOLS, plan_life_tool_calls
from app.gateway.costs import check_cost_cap
from app.gateway.service import GatewayCall, ModelGateway
from app.gateway.validation import validate_tool_calls
from app.services.model_call import log_model_call

MAX_TOOL_ROUNDS = 3


def _user_text(messages: Sequence[ChatMessage]) -> str:
    for message in reversed(messages):
        if message.role == "user" and message.content:
            return message.content
    return ""


def _reply_confirms_delivery(text: str, result: dict) -> bool:
    blob = (text or "").lower()
    if not blob:
        return False
    if any(
        token in blob
        for token in (
            "sent",
            "delivered",
            "texted",
            "called",
            "dialed",
            "reminder set",
            "reminded",
            "opened that",
            "on your screen",
            "couldn't set that reminder",
            "couldn't open that",
        )
    ):
        return True
    delivery = (result or {}).get("delivery") or {}
    evidence = delivery.get("evidence") or {}
    for value in (evidence.get("recipient"), evidence.get("to"), result.get("to")):
        if value and str(value).lower() in blob:
            return True
    return False


def _call_signature(call) -> tuple[str, str]:
    """Stable identity for a deterministic action-plan entry."""

    return call.name, json.dumps(call.arguments, sort_keys=True, default=str)


def planned_calls_for(
    messages: Sequence[ChatMessage],
    tool_specs: Sequence[ToolSpec],
    *,
    sensitive_allowed: bool = False,
):
    """Validated write-tool calls inferred from the owner's last utterance."""

    offered = {spec.name for spec in tool_specs}
    planned = plan_life_tool_calls(_user_text(messages), offered)
    if not planned:
        return []
    return [
        validated
        for validated in validate_tool_calls(
            planned, list(tool_specs), sensitive_allowed=sensitive_allowed
        )
        if validated.status in ("ok", "rectified")
    ]


async def run_tool_loop(
    session: AsyncSession,
    gateway: ModelGateway,
    messages: Sequence[ChatMessage],
    *,
    envelope,
    tool_specs: Sequence[ToolSpec],
    model: str | None = None,
    temperature: float = 0.7,
    actor: str,
    allow_sensitive_tools: bool = False,
    request_id: str | None = None,
) -> GatewayCall:
    """Run the model/tool loop for one turn with a hard round cap."""
    current_messages = list(messages)
    dispatched: set[str] = set()
    last_write_result: dict | None = None
    planned_injected = False
    preplanned_signatures: set[tuple[str, str]] = set()

    async def _cost_guard() -> None:
        await check_cost_cap(
            session,
            provider=gateway.provider.name,
            messages=current_messages,
        )

    gateway.cost_guard = _cost_guard

    async def dispatch_call(validated) -> None:
        nonlocal last_write_result
        arguments = validated.rectified_arguments or validated.call.arguments
        response = await ev_tools.dispatch(
            session,
            validated.call.name,
            arguments,
            actor=actor,
            allow_sensitive=allow_sensitive_tools,
            request_id=request_id,
        )
        dispatched.add(validated.call.name)
        if response.ok and response.result is not None:
            content = json.dumps(response.result, default=str)
            if validated.call.name in WRITE_TOOLS:
                last_write_result = {
                    "_tool": validated.call.name,
                    **(
                        response.result
                        if isinstance(response.result, dict)
                        else {"result": response.result}
                    ),
                }
        else:
            content = json.dumps(
                {"error": response.error or "tool failed"}, default=str
            )
        current_messages.append(
            ChatMessage(
                role="tool",
                content=content,
                name=validated.call.name,
            )
        )

    # OpenCode's session transport has no native function-call message. For an
    # explicit write request, execute the deterministic plan before asking the
    # model to phrase the confirmed result. Native providers retain the normal
    # model -> tool -> model protocol below.
    if not getattr(gateway.provider, "supports_tools", True):
        planned = planned_calls_for(
            current_messages,
            tool_specs,
            sensitive_allowed=allow_sensitive_tools,
        )
        for validated in planned:
            preplanned_signatures.add(_call_signature(validated.call))
            await dispatch_call(validated)
        planned_injected = bool(planned)

    call = await gateway.chat(
        current_messages,
        envelope=envelope,
        tools=tool_specs,
        model=model,
        temperature=temperature,
        allow_sensitive_tools=allow_sensitive_tools,
    )
    await log_model_call(session, call=call, actor=actor)
    if call.status == "degraded":
        call.result.text = (
            "EV's reasoning provider is temporarily unavailable or over its "
            "monthly budget. Memory, timeline, and recall still work offline."
        )
        return call

    for _ in range(MAX_TOOL_ROUNDS):
        executable = [
            validated
            for validated in call.tool_validation
            if validated.status in ("ok", "rectified")
            and _call_signature(validated.call) not in preplanned_signatures
        ]
        if not executable and not planned_injected:
            planned = [
                item
                for item in planned_calls_for(
                    current_messages,
                    tool_specs,
                    sensitive_allowed=allow_sensitive_tools,
                )
                if item.call.name not in dispatched
            ]
            if planned:
                executable = planned
                planned_injected = True
                call.tool_validation = list(call.tool_validation) + planned
        if not executable:
            break
        for validated in executable:
            await dispatch_call(validated)
        call = await gateway.chat(
            current_messages,
            envelope=envelope,
            tools=tool_specs,
            model=model,
            temperature=temperature,
            allow_sensitive_tools=allow_sensitive_tools,
        )
        await log_model_call(session, call=call, actor=actor)
        if call.status != "ok":
            break

    if call.status == "ok" and call.result.tool_calls and not call.result.text:
        call.result.text = (
            "I reached the tool-call limit for this turn. Ask me to continue "
            "and I'll keep going with the results I already gathered."
        )
    if last_write_result and not _reply_confirms_delivery(call.result.text, last_write_result):
        call.result.text = ev_tools.life_success_reply(
            last_write_result,
            tool_name=str(last_write_result.get("_tool") or ""),
        )
    return call
