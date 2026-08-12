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
from app.gateway.costs import check_cost_cap
from app.gateway.service import GatewayCall, ModelGateway
from app.services.model_call import log_model_call

MAX_TOOL_ROUNDS = 3


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

    async def _cost_guard() -> None:
        await check_cost_cap(
            session,
            provider=gateway.provider.name,
            messages=current_messages,
        )

    gateway.cost_guard = _cost_guard
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
        ]
        if not executable:
            break
        for validated in executable:
            arguments = validated.rectified_arguments or validated.call.arguments
            response = await ev_tools.dispatch(
                session,
                validated.call.name,
                arguments,
                actor=actor,
                allow_sensitive=allow_sensitive_tools,
                request_id=request_id,
            )
            if response.ok and response.result is not None:
                content = json.dumps(response.result, default=str)
            else:
                content = json.dumps({"error": response.error or "tool failed"}, default=str)
            current_messages.append(
                ChatMessage(
                    role="tool",
                    content=content,
                    name=validated.call.name,
                )
            )
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
    return call
