"""Lab brain helper: real brain through the existing gateway path.

guard_model_payload still applies (it runs inside ModelGateway.chat).
Echo/mock providers are refused. Every paid call is counted for the
spend cap; use spend_status() to read (calls, usd).
"""

from __future__ import annotations

import asyncio
from typing import Any

LAB_LINE = (
    "LAB: tools are disabled in this chat. If he asks you to do something, "
    "say plainly you cannot act here and help with words instead."
)

PAID_CALLS: list[dict[str, Any]] = []


def call_brain(system: str, user: str, history: list | None = None,
               *, temperature: float = 0.7,
               model: str | None = None) -> tuple[str, dict]:
    """One brain turn. Returns (reply_text, usage_dict). Never executes tools."""
    from app.contracts import ChatMessage
    from app.gateway.providers import get_chat_provider
    from app.gateway.service import ModelGateway

    provider = get_chat_provider()
    if provider.name in ("echo", "mock"):
        raise RuntimeError(f"refusing lab call on {provider.name!r}: real brain only")
    messages = [ChatMessage(role="system", content=system)]
    for role, text in history or []:
        messages.append(ChatMessage(role=role, content=text))
    messages.append(ChatMessage(role="user", content=user))
    call = asyncio.run(ModelGateway(provider).chat(messages, temperature=temperature,
                                                   model=model))
    if call.status != "ok":
        raise RuntimeError(f"brain call failed: {call.error}")
    raw_usage = call.usage() if callable(call.usage) else (call.usage or {})
    usage = dict(raw_usage or {})
    PAID_CALLS.append({"usage": usage, "model": getattr(call, "model", "")})
    return call.result.text, usage


def spend_status() -> tuple[int, float]:
    """(paid calls so far, reported USD so far) via the repo cost estimator."""
    total = 0.0
    try:
        from app.gateway.costs import reported_openrouter_cost

        for entry in PAID_CALLS:
            cost = reported_openrouter_cost(entry.get("usage"))
            if cost:
                total += float(cost)
    except Exception:
        pass
    return len(PAID_CALLS), total


def reset_spend() -> None:
    """Clear the counter (fresh measurement window)."""
    PAID_CALLS.clear()
