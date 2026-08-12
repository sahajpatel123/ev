"""Signed webhook backend: anything the human already carries."""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx

from app.config import settings
from app.notify.models import DeliveryReceipt, NotificationRecord, NotifierError


class WebhookNotifier:
    name = "webhook"

    async def send(self, notification: NotificationRecord) -> DeliveryReceipt:
        if not settings.notify_webhook_url:
            raise NotifierError("webhook backend unavailable: EV_NOTIFY_WEBHOOK_URL is not set")
        body = {
            "schema_version": "ev.notify.webhook.v1",
            "id": str(notification.id),
            "kind": notification.kind,
            "title": notification.title,
            "body": notification.body,
            "priority": notification.priority,
            "tier": notification.tier,
            "source": notification.source,
            "fingerprint": notification.fingerprint,
            "queued_at": notification.queued_at.isoformat() if notification.queued_at else None,
        }
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if settings.notify_webhook_secret:
            signature = hmac.new(
                settings.notify_webhook_secret.encode("utf-8"), raw, hashlib.sha256
            ).hexdigest()
            headers["X-EV-Signature"] = f"sha256={signature}"
        timeout = httpx.Timeout(10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                response = await client.post(
                    settings.notify_webhook_url, content=raw, headers=headers
                )
            except httpx.HTTPError as exc:
                raise NotifierError(f"webhook delivery failed: {type(exc).__name__}: {exc}") from exc
        if 200 <= response.status_code < 300:
            return DeliveryReceipt(
                status="delivered",
                backend=self.name,
                backend_ref=response.headers.get("X-EV-Delivery-Id") or str(notification.id),
                reason=None,
                details={"status_code": response.status_code},
            )
        raise NotifierError(
            f"webhook rejected: HTTP {response.status_code}: {response.text[:300]}"
        )
