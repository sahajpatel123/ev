"""Bounded Digital Operations watches — Gmail history + watched WhatsApp threads."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.digital.conditions import evaluate_digital
from app.digital.fabric import OpContext, execute
from app.digital.waiting import GLOBAL_WAITING
from app.models import PresenceCondition, PresenceContract
from app.presence.contract import ConditionState
from app.presence.service import resume_if_ready


async def digital_tick(session: AsyncSession) -> dict[str, Any]:
    """Evaluate pending digital conditions with bounded provider checks. No Spark."""
    conds = list(
        (
            await session.execute(
                select(PresenceCondition).where(
                    PresenceCondition.state == ConditionState.PENDING.value,
                    PresenceCondition.cond_class.in_(
                        [
                            "EMAIL_RECEIVED_MATCH",
                            "WHATSAPP_REPLY_MATCH",
                            "DOCUMENT_RECEIVED",
                            "PERSON_RESPONDED",
                            "CALENDAR_EVENT_CHANGED",
                        ]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    evaluated = 0
    resumed = 0
    for cond in conds[:20]:
        evaluated += 1
        ctx = await _context_for(session, cond)
        contract = await session.get(PresenceContract, cond.contract_id)
        did_resume = False
        if contract is not None and await resume_if_ready(session, contract, context=ctx):
            resumed += 1
            did_resume = True
        if (
            not did_resume
            and cond.state == ConditionState.PENDING.value
            and await evaluate_digital(session, cond, ctx)
        ):
            cond.state = ConditionState.SATISFIED.value
        person = str((cond.payload or {}).get("person") or "")
        blob = " ".join(
            str(m.get("snippet") or m.get("text") or "")
            for m in (ctx.get("messages") or [])
        )
        if did_resume or cond.state == ConditionState.SATISFIED.value:
            GLOBAL_WAITING.resolve_from_message(
                person=person or None,
                channel=str((cond.payload or {}).get("channel") or "") or None,
                text=blob,
                source_ref={"condition_id": str(cond.id)},
            )
    return {"evaluated": evaluated, "resumed": resumed, "spark_calls": 0}


async def _context_for(session: AsyncSession, cond: PresenceCondition) -> dict[str, Any]:
    payload = dict(cond.payload or {})
    ctx: dict[str, Any] = {"digital": {}, "messages": []}
    op_ctx = OpContext(session=session, actor="digital-watch")
    if cond.cond_class in {"EMAIL_RECEIVED_MATCH", "DOCUMENT_RECEIVED", "PERSON_RESPONDED"}:
        q = payload.get("query") or payload.get("q") or ""
        person = payload.get("person") or ""
        text = " ".join(x for x in (person, q) if x) or "in:inbox newer_than:2d"
        result = await execute("gmail", "search", {"text": text, "limit": 8}, ctx=op_ctx)
        msgs = result.payload.get("previews") or []
        ctx["messages"] = msgs
        ctx["gmail_events"] = msgs
        ctx["digital"]["gmail_events"] = msgs
        ctx["digital"]["messages"] = msgs
    if cond.cond_class in {"WHATSAPP_REPLY_MATCH", "PERSON_RESPONDED"}:
        chat = payload.get("chat_ref") or payload.get("person") or ""
        if chat:
            result = await execute("whatsapp", "read_recent", {"chat_ref": chat, "limit": 8}, ctx=op_ctx)
            content = result.payload.get("content") if isinstance(result.payload.get("content"), dict) else result.payload
            msgs = content.get("messages") or []
            ctx["whatsapp_events"] = msgs
            ctx["messages"] = list(ctx.get("messages") or []) + msgs
            ctx["digital"]["whatsapp_events"] = msgs
            ctx["digital"]["messages"] = ctx["messages"]
    return ctx
