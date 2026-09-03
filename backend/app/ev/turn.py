"""One-turn conductor: know the work, do requested actions, then one LLM reply.

OpenCode (opencode-go / deepseek-v4-flash) has no native function calling.
EV therefore executes write/life tools before the model speaks, injects a
WORKING ON snapshot plus action receipts, and asks the model only for wording.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import ChatMessage
from app.ev.briefing import WRITE_TOOLS, tools_for_turn
from app.ev.tools import dispatch, life_success_reply
from app.gateway.service import tool_specs_from_dicts
from app.services.tool_loop import planned_calls_for

_CONFIRM_TOKENS = (
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
    "couldn't finish that",
    "wrote",
    "created",
    "saved",
)


@dataclass(frozen=True)
class ActionReceipt:
    name: str
    ok: bool
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def as_payload(self) -> dict[str, Any]:
        payload = {"_tool": self.name, **self.result}
        if self.error and "error" not in payload:
            payload["error"] = self.error
        return payload


_PROMISE_RE = (
    "i'll",
    "i will",
    "let me",
    "going to",
    "i can send",
    "i can call",
    "i can set",
)


def snapshot_working_on(
    message: str,
    *,
    user_state: Any = None,
    conv_state: Any = None,
    continuation: bool = False,
) -> str:
    """Short block so every model call knows the live work of this turn."""

    request = (message or "").strip()[:400] or "(empty)"
    lines = ["WORKING ON", f"- This request: {request}"]
    focus = getattr(conv_state, "focus", None)
    if focus:
        lines.append(f"- Thread focus: {str(focus)[:200]}")
    working = getattr(conv_state, "working_context", None) or {}
    last_user = working.get("last_user_message") if isinstance(working, dict) else None
    if continuation and last_user:
        lines.append(f"- Previous owner line: {str(last_user)[:200]}")
    if user_state is not None:
        for label, value in (
            ("Current task", getattr(user_state, "current_task", None)),
            ("Active project", getattr(user_state, "active_project", None)),
            ("Active goal", getattr(user_state, "active_goal", None)),
            ("Activity", getattr(user_state, "activity", None)),
        ):
            if value:
                lines.append(f"- {label}: {str(value)[:200]}")
        topics = list(getattr(user_state, "recent_topics", None) or [])[:3]
        if topics:
            lines.append("- Recent topics: " + "; ".join(str(t)[:80] for t in topics))
    pending = list(getattr(conv_state, "pending_questions", None) or [])[:3]
    if continuation and pending:
        lines.append("- Open questions: " + "; ".join(str(q)[:80] for q in pending))
    if not continuation:
        lines.append("- Treat this request as self-contained unless it points at earlier turns.")
    return "\n".join(lines)


def operator_instructions(*, who: str, source: str) -> str:
    """Short operator rules. Flash does better with this than a long essay."""

    from app.ev.personality import SPEECH_STYLE_INSTRUCTIONS

    spoken = (
        f" SPOKEN TURN — you are {who}. One to two short, natural sentences. Keep it casual and brief."
        if source == "voice"
        else ""
    )
    return (
        f"You are {who}, the owner's operator. Keep replies casual, concise, and direct. "
        "Do not speak too much. State the answer plainly without repeating the question, "
        "rephrasing the same thought, or summarizing what was already said. "
        "This turn is request → actions (already run by EV when asked) → your reply. "
        "Stay on WORKING ON. If ACTIONS THIS TURN lists executed work, confirm "
        "the real result — never say you will do it later, never invent a "
        "success. If no actions ran, answer the current request directly. "
        "Lead with the answer. Prior memory is optional background."
        f"{spoken}"
        f"\n{SPEECH_STYLE_INSTRUCTIONS}"
    )


def format_action_receipts(receipts: list[ActionReceipt]) -> str:
    if not receipts:
        return "ACTIONS THIS TURN: none requested. Answer the current message."
    lines = ["ACTIONS THIS TURN (already executed by EV, not by you):"]
    for receipt in receipts:
        status = "ok" if receipt.ok else "failed"
        snippet = json.dumps(receipt.result or {"error": receipt.error}, default=str)
        if len(snippet) > 500:
            snippet = snippet[:497] + "..."
        lines.append(f"- {receipt.name}: {status} | {snippet}")
    lines.append("Confirm these results with evidence from the receipts. Do not re-do them.")
    return "\n".join(lines)


def receipt_messages(receipts: list[ActionReceipt]) -> list[ChatMessage]:
    """Tool-role messages so the wording model (and OpenCode-shaped tests) see results."""

    messages: list[ChatMessage] = []
    for receipt in receipts:
        content = json.dumps(receipt.result or {"error": receipt.error}, default=str)
        messages.append(
            ChatMessage(role="tool", content=content, name=receipt.name)
        )
    return messages


def writes_ran(receipts: list[ActionReceipt]) -> bool:
    return any(receipt.name in WRITE_TOOLS for receipt in receipts)


def presented_this_turn(receipts: list[ActionReceipt]) -> bool:
    return any(receipt.name == "present" and receipt.ok for receipt in receipts)


def reply_confirms_action(text: str, payload: dict[str, Any]) -> bool:
    blob = (text or "").lower()
    if not blob:
        return False
    promised = any(token in blob for token in _PROMISE_RE)
    confirmed = any(token in blob for token in _CONFIRM_TOKENS)
    if promised and not confirmed:
        return False
    if confirmed:
        return True
    delivery = payload.get("delivery") or {}
    evidence = delivery.get("evidence") or {}
    for value in (evidence.get("recipient"), evidence.get("to"), payload.get("to")):
        if value and str(value).lower() in blob and not promised:
            return True
    return False


def confirmed_reply(text: str, receipts: list[ActionReceipt]) -> str:
    """Keep model wording when it confirms; otherwise speak the real receipt."""

    writes = [receipt for receipt in receipts if receipt.name in WRITE_TOOLS]
    if not writes:
        return text
    last = writes[-1]
    payload = last.as_payload()
    if reply_confirms_action(text, payload):
        return text
    return life_success_reply(payload, tool_name=last.name)


def build_system_prompt(
    *,
    identity: str,
    who: str,
    source: str,
    working_on: str,
    context: str,
    briefing: str | None,
    receipts: list[ActionReceipt],
) -> str:
    parts = [
        identity.strip(),
        operator_instructions(who=who, source=source),
        working_on.strip(),
        format_action_receipts(receipts),
    ]
    if (context or "").strip():
        parts.append(context.strip())
    if (briefing or "").strip():
        parts.append(briefing.strip())
    return "\n\n".join(part for part in parts if part)


async def execute_requested_actions(
    session: AsyncSession,
    message: str,
    *,
    actor: str,
    allow_sensitive: bool,
    request_id: str | None = None,
    device_id=None,
    live_session_id: str | None = None,
) -> list[ActionReceipt]:
    """Dispatch write/life tools the owner asked for, before the LLM speaks."""

    from app.ev.luna_code import (
        last_code_job,
        looks_like_code_continue,
        looks_like_code_followup,
        spoken_code_followup,
    )

    job = last_code_job(str(live_session_id or "")) or last_code_job()
    if looks_like_code_followup(message):
        if job:
            spoken = spoken_code_followup(message, job)
            if spoken:
                return [
                    ActionReceipt(
                        name="code",
                        ok=True,
                        result={"ok": True, "spoken": spoken, "workspace": job.get("workspace")},
                    )
                ]
    if looks_like_code_continue(message) and job:
        response = await dispatch(
            session,
            "code",
            {"goal": message},
            actor=actor,
            allow_sensitive=allow_sensitive,
            request_id=request_id,
            device_id=device_id,
            live_session_id=live_session_id,
            channel="voice" if actor == "voice" else "action",
        )
        result = response.result if isinstance(response.result, dict) else {"ok": response.ok}
        return [
            ActionReceipt(
                name="code",
                ok=bool(response.ok and result.get("ok", True)),
                result=result,
            )
        ]

    specs = tool_specs_from_dicts(tools_for_turn(message))
    planned = planned_calls_for(
        [ChatMessage(role="user", content=message)],
        specs,
        sensitive_allowed=allow_sensitive,
    )
    receipts: list[ActionReceipt] = []
    for validated in planned:
        arguments = validated.rectified_arguments or validated.call.arguments
        response = await dispatch(
            session,
            validated.call.name,
            arguments,
            actor=actor,
            allow_sensitive=allow_sensitive,
            request_id=request_id,
            device_id=device_id,
            live_session_id=live_session_id,
            channel="voice" if actor == "voice" else "action",
        )
        if isinstance(response.result, dict):
            result = response.result
        elif response.result is not None:
            result = {"result": response.result}
        else:
            result = {}
        receipts.append(
            ActionReceipt(
                name=validated.call.name,
                ok=bool(response.ok),
                result=result,
                error=response.error,
            )
        )
    return receipts
