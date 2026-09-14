"""Phone PWA digital-operations intercept — goals continue if the phone closes."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.digital.fabric import OpContext, answer_can_you
from app.digital.orchestrate import handle_outcome
from app.digital.phone_brief import is_phone_comm_ask, phone_inbox_turn, public_brief
from app.digital.waiting import GLOBAL_WAITING, owner_brief

_DIGITAL = re.compile(
    r"(?i)\b("
    r"gmail|whatsapp|inbox|email|e-mail|"
    r"imessage|i-message|"
    r"waiting on|waiting for|"
    r"what can you currently do|"
    r"what can'?t you do|"
    r"prepare me for|"
    r"final (?:price|quote)|"
    r"who am i supposed to reply|"
    r"did i promise|"
    r"find the (?:email|pdf|invoice|quotation|message)"
    r")\b"
)


async def maybe_digital_turn(
    session: AsyncSession,
    text: str,
    *,
    device: Any = None,
    ctx: OpContext | None = None,
    imessage_peek: Any = None,
) -> dict[str, Any] | None:
    blob = (text or "").strip()
    if not blob:
        return None
    device_id = str(getattr(device, "id", "") or "")
    if re.search(r"(?i)what can you currently|what can'?t you|what can you (?:do|currently do) with\b", blob):
        ans = answer_can_you(blob)
        return {
            "reply": ans.get("spoken"),
            "ok": True,
            "route": "DIGITAL_OPS",
            "operation": "capabilities",
            "route_target": "CORE",
            "digital": ans,
        }
    if re.search(r"(?i)who am i waiting|what am i waiting|waiting on me|supposed to reply|did i promise", blob):
        brief = owner_brief(GLOBAL_WAITING)
        return {
            "reply": brief["spoken"],
            "ok": True,
            "route": "DIGITAL_OPS",
            "operation": "waiting",
            "route_target": "CORE",
            "digital": brief,
        }
    if is_phone_comm_ask(blob, device_id=device_id) or _DIGITAL.search(blob):
        if is_phone_comm_ask(blob, device_id=device_id):
            result = await phone_inbox_turn(
                session, blob, device=device, ctx=ctx, imessage_peek=imessage_peek
            )
            spoken = result.get("spoken") or "I can summarize mail, WhatsApp, or Messages — not dump the thread."
            return {
                "reply": spoken,
                "ok": True,
                "route": "DIGITAL_OPS",
                "operation": result.get("manner") or "phone_brief",
                "route_target": "CORE",
                "phone_may_close": True,
                "second_prompt_required": False,
                "digital": public_brief(result),
                "device_id": device_id,
            }
        ctx = ctx or OpContext(actor="phone", session=session)
        result = await handle_outcome(blob, ctx=ctx)
        spoken = result.get("spoken")
        if not spoken:
            spoken = f"Digital operations: {result.get('kind')} ({result.get('status')})."
            if result.get("sent") is False and result.get("status") == "PREPARED":
                spoken = "Prepared — not sent. Approve to send."
            if result.get("status") == "CLARIFY":
                spoken = "I need you to pick the person. I will not guess the recipient."
            if result.get("status") == "SERVICE_AUTH_REQUIRED":
                spoken = "That service needs a one-time owner connection. See the connection pack."
        return {
            "reply": spoken,
            "ok": True,
            "route": "DIGITAL_OPS",
            "operation": result.get("kind") or "outcome",
            "route_target": "CORE",
            "phone_may_close": True,
            "second_prompt_required": False,
            "digital": result,
            "device_id": device_id,
        }
    return None
