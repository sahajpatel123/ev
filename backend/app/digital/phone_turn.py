"""Phone PWA digital-operations intercept — goals continue if the phone closes."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.digital.fabric import OpContext, answer_can_you
from app.digital.orchestrate import handle_outcome
from app.digital.waiting import GLOBAL_WAITING, owner_brief

_DIGITAL = re.compile(
    r"(?i)\b("
    r"gmail|whatsapp|inbox|email|e-mail|"
    r"waiting on|waiting for|"
    r"what can you (?:currently )?do|"
    r"what can'?t you do|"
    r"prepare me for|"
    r"final (?:price|quote)|"
    r"who am i supposed to reply|"
    r"did i promise|"
    r"find the (?:email|pdf|invoice|quotation|message)"
    r")\b"
)


async def maybe_digital_turn(session: AsyncSession, text: str, *, device: Any = None) -> dict[str, Any] | None:
    blob = (text or "").strip()
    if not blob or not _DIGITAL.search(blob):
        return None
    if re.search(r"(?i)what can you|what can'?t you", blob):
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
    ctx = OpContext(actor="phone", session=session)
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
        "device_id": str(getattr(device, "id", "") or ""),
    }
