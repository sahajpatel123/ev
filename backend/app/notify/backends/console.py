"""Console/log backend: the CI double."""

from __future__ import annotations

import json

from app.notify.models import DeliveryReceipt, NotificationRecord


class ConsoleNotifier:
    name = "console"

    async def send(self, notification: NotificationRecord) -> DeliveryReceipt:
        payload = {
            "id": str(notification.id),
            "kind": notification.kind,
            "title": notification.title,
            "body": notification.body,
            "priority": notification.priority,
            "tier": notification.tier,
            "source": notification.source,
            "fingerprint": notification.fingerprint,
        }
        print(f"[ev.notify] {json.dumps(payload, ensure_ascii=False)}", flush=True)
        return DeliveryReceipt(
            status="delivered",
            backend=self.name,
            backend_ref=str(notification.id),
            reason=None,
            details={"channel": "stdout", "schema_version": "ev.notify.receipt.v1"},
        )
