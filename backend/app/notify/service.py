"""Delivery service: dispatch, receipts, alert radar wiring, digests, DLQ."""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.ev import alert_radar
from app.ev.ev_sense import quiet_hours_active
from app.models import Alert, ApprovedAction, DeadLetter, Notification
from app.notify.backends import get_backend
from app.notify.models import DeliveryReceipt, NotificationRecord, NotifierError
from app.notify.policy import decide, is_emergency
from app.utils.text import sha256_hex, utcnow


async def _record_event(session: AsyncSession, row: Notification) -> None:
    from app.services.runtime import record_runtime_event

    await record_runtime_event(
        session,
        kind="notification",
        payload={
            "notification_id": str(row.id),
            "kind": row.kind,
            "status": row.status,
            "reason": row.reason,
            "backend": row.backend,
            "title": row.title,
            "priority": row.priority,
            "tier": row.tier,
        },
        action_id=row.action_id,
    )


async def _update_alert(session: AsyncSession, row: Notification) -> None:
    """Propagate a receipt to the originating Alert row without lying."""
    if row.alert_id is None:
        return
    alert = await session.get(Alert, row.alert_id)
    if alert is None:
        return
    details = dict(alert.details or {})
    if row.status == "delivered":
        alert.status = "delivered"
        alert.delivered_at = row.delivered_at or utcnow()
        details["delivery_receipt"] = {
            "notification_id": str(row.id),
            "backend": row.backend,
            "backend_ref": row.backend_ref,
            "delivered_at": (row.delivered_at or utcnow()).isoformat(),
        }
    elif row.status == "suppressed":
        if row.reason == "max_attempts":
            alert.status = "failed"
            details["failure_reason"] = row.reason
        else:
            alert.status = "suppressed"
            details["suppression_reason"] = row.reason
    elif row.status == "failed":
        attempts = int(details.get("notify_attempts", 0)) + 1
        details["notify_attempts"] = attempts
        details["last_error"] = row.reason
        if attempts >= settings.notify_max_attempts:
            alert.status = "failed"
            details["failure_reason"] = row.reason
    alert.details = details
    await session.flush()


async def dispatch_notification(
    session: AsyncSession,
    *,
    title: str,
    body: str,
    priority: float = 0.5,
    tier: str = "background",
    kind: str = "general",
    source: str | None = None,
    fingerprint: str | None = None,
    alert_id: UUID | None = None,
    action_id: UUID | None = None,
    emergency: bool = False,
    allow_during_quiet_hours: bool = False,
    bypass_policy: bool = False,
    backend_override: str | None = None,
    details: dict | None = None,
    now=None,
) -> Notification:
    """Create a delivery-ledger row and dispatch through the configured backend.

    Status is set only from the backend receipt (or the attention policy);
    a caller-supplied result is never treated as delivery evidence.
    """
    now = now or utcnow()
    fp = fingerprint or sha256_hex(f"{kind}:{title}:{body}")[:64]
    row = Notification(
        kind=kind,
        title=title,
        body=body,
        priority=priority,
        tier=tier,
        source=source,
        fingerprint=fp,
        status="attempted",
        alert_id=alert_id,
        action_id=action_id,
        queued_at=now,
        attempt_count=1,
        details={**(details or {}), "policy": {"bypass_policy": bypass_policy}},
    )
    session.add(row)
    await session.flush()

    effective_emergency = is_emergency(priority=priority, tier=tier, emergency=emergency)
    decision = await decide(
        session,
        fingerprint=fp,
        exclude_id=row.id,
        priority=priority,
        tier=tier,
        emergency=effective_emergency,
        allow_during_quiet_hours=allow_during_quiet_hours,
        bypass_policy=bypass_policy,
        now=now,
    )
    row.details = {**row.details, "policy": {**row.details["policy"], "decision": decision.reason}}
    if not decision.allowed:
        row.status = "suppressed"
        row.reason = decision.reason
        row.last_attempt_at = now
        await session.flush()
        await _record_event(session, row)
        await _update_alert(session, row)
        return row

    backend_name = backend_override or settings.notify_backend
    record = NotificationRecord(
        id=row.id,
        kind=row.kind,
        title=row.title,
        body=row.body,
        priority=row.priority,
        tier=row.tier,
        source=row.source,
        fingerprint=row.fingerprint,
        queued_at=now,
        details={**(details or {}), "policy": row.details.get("policy", {})},
    )
    backend = get_backend(backend_name)
    try:
        receipt = await backend.send(record)
    except NotifierError as exc:
        receipt = DeliveryReceipt(
            status="failed",
            backend=backend_name,
            reason=str(exc),
            details={"error_type": type(exc).__name__},
        )
    except Exception as exc:  # noqa: BLE001 - backend boundary: honest failure
        receipt = DeliveryReceipt(
            status="failed",
            backend=backend_name,
            reason=f"{type(exc).__name__}: {exc}",
            details={"error_type": type(exc).__name__},
        )
    row.backend = receipt.backend
    row.backend_ref = receipt.backend_ref
    row.last_attempt_at = now
    row.status = receipt.status
    row.reason = receipt.reason
    row.details = {**row.details, "receipt": receipt.details}
    if receipt.status == "delivered":
        row.delivered_at = now
    await session.flush()
    await _record_event(session, row)
    await _update_alert(session, row)
    return row


async def dispatch_action(session: AsyncSession, action: ApprovedAction) -> Notification | None:
    """Dispatch the notification/send_message actions through real backends.

    These actions require explicit human approval before execution, so they
    are treated as human-declared and bypass quiet hours/cap; the receipt is
    still recorded so nothing is claimed delivered without backend evidence.
    """
    payload = action.payload or {}
    if action.action_type == "notification":
        title = payload.get("title") or action.title or "EV notification"
        body = payload.get("text") or payload.get("body") or ""
        priority = float(payload.get("priority", 0.5))
        return await dispatch_notification(
            session,
            title=title,
            body=body,
            priority=priority,
            tier="urgent" if priority >= settings.notify_emergency_priority_threshold else "useful",
            kind="action:notification",
            source="action:notification",
            fingerprint=f"action:{action.id}",
            action_id=action.id,
            emergency=True,
            bypass_policy=True,
        )
    if action.action_type == "send_message":
        channel = payload.get("channel", "default")
        target = payload.get("to")
        text = payload.get("text", "")
        body = f"[{channel}" + (f" -> {target}" if target else "") + f"] {text}"
        backend_override = "webhook" if channel in ("sms", "chat", "whatsapp", "telegram") else None
        return await dispatch_notification(
            session,
            title=f"EV message via {channel}",
            body=body,
            priority=0.5,
            tier="useful",
            kind="action:message",
            source="action:send_message",
            fingerprint=f"action:{action.id}",
            action_id=action.id,
            emergency=True,
            bypass_policy=True,
            backend_override=backend_override,
            details={"channel": channel, "to": target},
        )
    return None


async def deliver_pending_alerts(
    session: AsyncSession, *, now=None
) -> dict:
    """Deliver pending alert-radar rows (watch, EV Sense, gear, routines).

    Non-emergency rows during quiet hours are deliberately left pending so the
    daemon's digest batches them; urgent rows go out immediately.
    """
    now = now or utcnow()
    quiet = quiet_hours_active(now)
    rows = list(
        (
            await session.execute(
                select(Alert)
                .where(Alert.status == "pending")
                .order_by(Alert.priority.desc(), Alert.created_at.asc())
                .limit(200)
            )
        ).scalars().all()
    )
    counts = {"delivered": 0, "suppressed": 0, "failed": 0, "skipped": 0}
    for alert in rows:
        emergency = is_emergency(priority=alert.priority, tier=alert.tier, emergency=False)
        if quiet and not emergency:
            counts["skipped"] += 1
            continue
        row = await dispatch_notification(
            session,
            title=alert.title,
            body=alert.body,
            priority=alert.priority,
            tier=alert.tier,
            kind="alert",
            source=alert.source,
            fingerprint=f"alert:{alert.fingerprint}",
            alert_id=alert.id,
            emergency=emergency,
        )
        counts[row.status] = counts.get(row.status, 0) + 1
    return counts


async def build_and_deliver_digest(
    session: AsyncSession, *, now=None
) -> dict | None:
    """Batch pending non-urgent alerts into one delivered quiet-hours digest.

    Alerts are only marked delivered after the digest notification receipt
    proves backend delivery. Suppressed digests carry a reason; failed digests
    leave alerts pending for the next tick.
    """
    pending = [
        alert
        for alert in await alert_radar.list_alerts(session, status="pending", limit=200)
        if alert.tier in ("useful", "background")
    ]
    if not pending:
        return None
    digest_id = uuid4().hex
    lines = [
        f"{index}. {alert.title}"
        + (f" — {alert.body}" if alert.body and alert.body != alert.title else "")
        for index, alert in enumerate(pending[:50], start=1)
    ]
    row = await dispatch_notification(
        session,
        title="EV quiet-hours digest",
        body="\n".join(lines),
        priority=0.5,
        tier="digest",
        kind="digest",
        source="runtime:digest",
        fingerprint=f"digest:{utcnow().date().isoformat()}",
        emergency=False,
        allow_during_quiet_hours=True,
        details={"digest_id": digest_id, "alert_ids": [str(a.id) for a in pending]},
        now=now,
    )
    delivered = 0
    suppressed = 0
    for alert in pending:
        details = dict(alert.details or {})
        if row.status == "delivered":
            alert.status = "delivered"
            alert.delivered_at = row.delivered_at or utcnow()
            details["delivery"] = "digest"
            details["digest_id"] = digest_id
            details["delivery_receipt"] = {
                "notification_id": str(row.id),
                "backend": row.backend,
                "delivered_at": (row.delivered_at or utcnow()).isoformat(),
            }
            delivered += 1
        elif row.status == "suppressed":
            alert.status = "suppressed"
            details["suppression_reason"] = row.reason
            suppressed += 1
        alert.details = details
    await session.flush()
    return {
        "schema_version": "ev.runtime.digest.v1",
        "digest_id": digest_id,
        "generated_at": utcnow().isoformat(),
        "delivered": delivered,
        "suppressed": suppressed,
        "failed": 1 if row.status == "failed" else 0,
        "alerts": [
            {
                "id": str(alert.id),
                "title": alert.title,
                "body": alert.body,
                "priority": alert.priority,
                "tier": alert.tier,
            }
            for alert in pending
        ],
    }


async def deliver_dlq_escalations(
    session: AsyncSession, *, now=None
) -> list[Notification]:
    """Escalate permanently discarded dead letters as auditable notifications."""
    rows = list(
        (
            await session.execute(
                select(DeadLetter).where(DeadLetter.status == "discarded")
            )
        ).scalars().all()
    )
    sent: list[Notification] = []
    for letter in rows:
        fingerprint = f"dlq:{letter.id}:discarded"
        existing = (
            await session.execute(
                select(Notification.id).where(
                    Notification.fingerprint == fingerprint,
                    Notification.status.in_(["delivered", "suppressed"]),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        body = (
            f"Job {letter.job_id or 'unknown'} in queue {letter.queue} was discarded "
            f"after {letter.attempts} attempts: {letter.error}"
        )
        sent.append(
            await dispatch_notification(
                session,
                title="EV lost a background job permanently",
                body=body,
                priority=0.6,
                tier="useful",
                kind="dead_letter",
                source=f"dlq:{letter.id}",
                fingerprint=fingerprint,
                emergency=False,
                now=now,
            )
        )
    return sent


async def notify_status(session: AsyncSession, *, now=None) -> dict:
    now = now or utcnow()
    backend_name = settings.notify_backend
    available = True
    reason: str | None = None
    permission: str | None = None
    try:
        backend = get_backend(backend_name)
        if hasattr(backend, "check_permission"):
            permission = (await backend.check_permission()).get("permission")
            if permission == "denied":
                available = False
                reason = "macOS notification permission denied"
    except NotifierError as exc:
        available = False
        reason = str(exc)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    counts = {}
    for status in ("delivered", "suppressed", "failed"):
        counts[status] = int(
            (
                await session.execute(
                    select(func.count(Notification.id)).where(
                        Notification.status == status,
                        Notification.queued_at >= start_of_day,
                    )
                )
            ).scalar_one()
        )
    return {
        "backend": backend_name,
        "available": available,
        "reason": reason,
        "permission": permission,
        "delivered_today": counts["delivered"],
        "suppressed_today": counts["suppressed"],
        "failed_today": counts["failed"],
    }
