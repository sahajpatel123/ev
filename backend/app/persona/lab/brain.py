"""Lab brain helper: real brain through the existing gateway path.

guard_model_payload still applies (it runs inside ModelGateway.chat).
Echo/mock providers are refused. Every paid call is counted for the
spend cap; use spend_status() to read (calls, usd).

max_tokens set -> raw OpenRouter call with an explicit cap (same model,
temperature, and prompts; the gateway sends no max_tokens, which makes
OpenRouter pre-authorize 65536 output tokens and 402 once the balance
drops below that worst case). The raw path keeps the echo/mock refusal
and the chat_egress compliance check; generation cost is looked up for
accounting. Caps are picked far above observed maxima and audited via
finish_reason after the run.
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
               model: str | None = None,
               max_tokens: int | None = None) -> tuple[str, dict]:
    """One brain turn. Returns (reply_text, usage_dict). Never executes tools."""
    if max_tokens is not None:
        return _call_brain_capped(system, user, history,
                                  temperature=temperature, model=model,
                                  max_tokens=max_tokens)
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


def _call_brain_capped(system: str, user: str, history: list | None,
                       *, temperature: float, model: str | None,
                       max_tokens: int) -> tuple[str, dict]:
    """Raw OpenRouter call with an explicit max_tokens cap.

    The gateway sends no max_tokens, so OpenRouter pre-authorizes 65536
    output tokens; on expensive models that 402s once the balance drops
    below the worst-case estimate (seen live at $1.24 remaining, with the
    API itself advising "fewer max_tokens"). Same model, temperature, and
    prompts as the gateway path — the cap only binds outputs that would
    otherwise exceed it, and every caller picks a cap far above observed
    maxima (audited via finish_reason after the run).
    """
    import json
    import urllib.request

    from app.compliance.policy import remote_processing_allowed
    from app.config import settings
    from app.gateway.providers import get_chat_provider

    provider = get_chat_provider()
    if provider.name in ("echo", "mock"):
        raise RuntimeError(f"refusing lab call on {provider.name!r}: real brain only")
    if not remote_processing_allowed("chat_egress"):
        raise RuntimeError(
            "The brain is blocked: EV_ALLOW_REMOTE_CHAT is not enabled")
    api_key = settings.openrouter_api_key or ""
    if not api_key.strip():
        raise RuntimeError("EV_OPENROUTER_API_KEY is not set")
    from app.gateway.openrouter_muse import default_brain_model

    default_model = default_brain_model()
    messages = [{"role": "system", "content": system}]
    for role, text in history or []:
        messages.append({"role": role, "content": text})
    messages.append({"role": "user", "content": user})
    body = json.dumps({
        "model": model or default_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "usage": {"include": True},
    }).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions", data=body,
        headers={"Authorization": "Bearer " + api_key,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:300]
        raise RuntimeError(
            f"brain call failed: HTTP {exc.code}: {detail}") from exc
    choice = (payload.get("choices") or [{}])[0]
    text = (choice.get("message") or {}).get("content") or ""
    finish = choice.get("finish_reason")
    if finish == "length":
        # Never silently truncate: callers retry (judge) or fail loud (gen).
        raise RuntimeError(
            f"brain call truncated at max_tokens={max_tokens}") from None
    tokens = payload.get("usage") or {}
    usage = {
        "prompt_tokens": tokens.get("prompt_tokens", 0),
        "completion_tokens": tokens.get("completion_tokens", 0),
        "total_tokens": tokens.get("total_tokens", 0),
        "cost": float(tokens.get("cost") or 0.0),
        "finish_reason": finish,
        "capped": True,
    }
    PAID_CALLS.append({"usage": usage, "model": model or default_model})
    return text, usage


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
