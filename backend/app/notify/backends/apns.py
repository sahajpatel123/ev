"""APNs backend — written but inert until Agent 18 registers a device token.

Nothing here performs network I/O unless ``EV_NOTIFY_APNS_ENABLED=true`` AND a
token is present AND an ES256 signing key is available. Until then every send
fails honestly with a reason instead of pretending to deliver.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx

from app.config import settings
from app.notify.models import DeliveryReceipt, NotificationRecord, NotifierError


class APNsNotifier:
    name = "apns"

    async def send(self, notification: NotificationRecord) -> DeliveryReceipt:
        if not settings.notify_apns_enabled:
            raise NotifierError("apns_inert: EV_NOTIFY_APNS_ENABLED is false")
        token = (notification.details or {}).get("device_token")
        if not token:
            raise NotifierError(
                "apns_inert: no device token registered (Agent 18 app not onboarded)"
            )
        if not (
            settings.notify_apns_key_path
            and settings.notify_apns_key_id
            and settings.notify_apns_team_id
            and settings.notify_apns_topic
        ):
            raise NotifierError("apns_inert: key path/id/team/topic not configured")
        try:
            import cryptography
        except ImportError:
            raise NotifierError(
                "apns_inert: cryptography package unavailable for ES256 signing"
            ) from None
        _ = cryptography  # signing path is implemented in a later Agent 18 milestone
        payload = {
            "aps": {
                "alert": {"title": notification.title, "body": notification.body},
                "sound": "default",
                "thread-id": notification.kind,
            },
            "ev": {
                "id": str(notification.id),
                "kind": notification.kind,
                "priority": notification.priority,
                "fingerprint": notification.fingerprint,
            },
        }
        # Constructed but deliberately not sent: Agent 18 owns the app-side
        # registration and the push-via-Tailscale decision. Honest gap.
        _ = json.dumps(payload)
        _ = datetime.now(UTC) + timedelta(hours=1)
        _ = httpx
        raise NotifierError(
            "apns_inert: agent 18 token registration has not landed; no send attempted"
        )
