"""Phone push delivery: deliver inbox items to APNs when the phone
registered a token and Home Station holds APNs credentials.

APNs credentials are not required to run: without them delivery stays
"poll" and the inbox item records the honest reason (apns_inert: ...).
Nothing is claimed as pushed that was not accepted by Apple.
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Device


async def attempt_push(
    session: AsyncSession,
    *,
    device: Device,
    title: str,
    body: str,
    kind: str = "notice",
    item_id: str,
) -> dict:
    """Try an APNs delivery for one inbox item; never raises."""
    profile = dict(getattr(device, "endpoint_profile", None) or {})
    note = profile.get("notifications") or {}
    token = str(getattr(device, "push_token", None) or "").strip()
    if not token or str(note.get("delivery") or "poll").lower() != "apns":
        return {"state": "poll", "reason": "no_apns_registration"}
    if not settings.notify_apns_enabled:
        return {"state": "poll", "reason": "apns_disabled"}
    try:
        from app.notify.backends.apns import APNsNotifier
        from app.notify.models import NotificationRecord

        receipt = await APNsNotifier().send(
            NotificationRecord(
                id=uuid4(),
                kind=kind,
                title=str(title or "")[:160],
                body=str(body or "")[:1900],
                priority=0.3,
                tier="background",
                source="device_gateway.inbox",
                fingerprint=f"inbox:{item_id}",
                details={"device_token": token},
            )
        )
    except Exception as exc:  # noqa: BLE001 - honest degraded fallback
        return {"state": "poll", "reason": f"{type(exc).__name__}: {exc}"[:300]}
    return {
        "state": "delivered" if getattr(receipt, "status", None) == "delivered" else "poll",
        "backend": getattr(receipt, "backend", None),
        "backend_ref": getattr(receipt, "backend_ref", None),
        "reason": getattr(receipt, "reason", None),
    }
