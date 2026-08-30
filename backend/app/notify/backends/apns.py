"""APNs backend — wired end-to-end, gated on real configuration.

The backend performs network I/O only when ``EV_NOTIFY_APNS_ENABLED=true``,
a device token is present, and the ES256 key path/key id/team id/topic are
configured. Every missing piece fails honestly with an actionable reason;
``device_token`` comes from the registry (SUIT's ``POST /v1/devices/{id}/push-token``).
"""

from __future__ import annotations

import base64
import json
import time

import httpx

from app.config import settings
from app.notify.models import DeliveryReceipt, NotificationRecord, NotifierError


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _der_integer(data: bytes, offset: int) -> tuple[int, int]:
    """Read one DER INTEGER (positive, no leading zero) and return (value, next)."""
    if data[offset] != 0x02:
        raise NotifierError("apns_inert: invalid DER signature from signing key")
    length = data[offset + 1]
    value = int.from_bytes(data[offset + 2 : offset + 2 + length], "big")
    return value, offset + 2 + length


def _es256_jwt(
    *,
    key_path: str,
    key_id: str,
    team_id: str,
) -> str:
    """Build an APNs provider token (ES256) without third-party packages."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    header = {"alg": "ES256", "kid": key_id}
    claims = {"iss": team_id, "iat": int(time.time())}
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(claims, separators=(",", ":")).encode())
    )
    with open(key_path, "rb") as handle:
        key = serialization.load_pem_private_key(handle.read(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise NotifierError("apns_inert: signing key is not an EC private key")
    der = key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256()))
    r_value, offset = _der_integer(der, 0)
    s_value, _ = _der_integer(der, offset)
    width = 32
    signature = r_value.to_bytes(width, "big") + s_value.to_bytes(width, "big")
    return signing_input + "." + _b64url(signature)


class APNsNotifier:
    name = "apns"

    async def send(self, notification: NotificationRecord) -> DeliveryReceipt:
        if not settings.notify_apns_enabled:
            raise NotifierError("apns_inert: EV_NOTIFY_APNS_ENABLED is false")
        token = (notification.details or {}).get("device_token")
        if not token:
            raise NotifierError(
                "apns_inert: no device token registered (SUIT app not onboarded)"
            )
        if not (
            settings.notify_apns_key_path
            and settings.notify_apns_key_id
            and settings.notify_apns_team_id
            and settings.notify_apns_topic
        ):
            raise NotifierError("apns_inert: key path/id/team/topic not configured")
        try:
            import cryptography  # noqa: F401
        except ImportError:
            raise NotifierError(
                "apns_inert: cryptography package unavailable for ES256 signing"
            ) from None

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
        try:
            jwt = _es256_jwt(
                key_path=settings.notify_apns_key_path,
                key_id=settings.notify_apns_key_id,
                team_id=settings.notify_apns_team_id,
            )
        except NotifierError:
            raise
        except Exception as exc:  # noqa: BLE001 - signing boundary: honest failure
            raise NotifierError(
                f"apns_inert: ES256 signing failed: {type(exc).__name__}: {exc}"
            ) from exc

        url = f"https://api.push.apple.com/3/device/{token}"
        headers = {
            "authorization": f"bearer {jwt}",
            "apns-topic": settings.notify_apns_topic,
            "apns-push-type": "alert",
            "apns-priority": "10",
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise NotifierError(
                f"apns delivery failed: {type(exc).__name__}: {exc}"
            ) from exc
        if response.status_code == 200:
            return DeliveryReceipt(
                status="delivered",
                backend=self.name,
                backend_ref=response.headers.get("apns-id") or str(notification.id),
                reason=None,
                details={
                    "status_code": 200,
                    "apns_id": response.headers.get("apns-id"),
                },
            )
        raise NotifierError(
            f"apns rejected: HTTP {response.status_code}: {response.text[:300]}"
        )
