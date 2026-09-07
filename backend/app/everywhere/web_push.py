"""Web Push (VAPID) for the PWA — one honest delivery lane for inbox items.

The inbox is DB-only today: a paired phone learns about a nudge the next
time it polls. Web Push closes that gap for installed PWAs (iOS 16.4+/Safari
standalone, Android, desktop) without touching the frozen Mac surface.

Design laws:
- Best-effort and non-blocking. A push failure NEVER fails the inbox write.
- Keys come from settings (EV_WEB_PUSH_*). Unconfigured → the whole module
  is a no-op; nothing crashes, nothing sends.
- Subscriptions live in the device's endpoint_profile (additive JSON, no
  migration). A 404/410 from the push service clears the dead subscription.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.config import settings
from app.models import Device

logger = logging.getLogger(__name__)


def vapid_configured() -> bool:
    return bool(
        (settings.web_push_vapid_private_key or "").strip()
        and (settings.web_push_vapid_public_key or "").strip()
    )


def vapid_public_key() -> str:
    """Raw applicationServerKey for the browser subscribe() call."""

    return (settings.web_push_vapid_public_key or "").strip()


def web_subscription(device: Device) -> dict[str, Any] | None:
    profile = getattr(device, "endpoint_profile", None)
    if not isinstance(profile, dict):
        return None
    sub = profile.get("web_push")
    if not isinstance(sub, dict) or not str(sub.get("endpoint") or "").startswith("https://"):
        return None
    keys = sub.get("keys")
    if not isinstance(keys, dict) or not keys.get("p256dh") or not keys.get("auth"):
        return None
    return sub


def store_web_subscription(device: Device, *, endpoint: str, keys: dict[str, str]) -> dict[str, Any]:
    profile = dict(getattr(device, "endpoint_profile", None) or {})
    profile["web_push"] = {
        "endpoint": (endpoint or "")[:1024],
        "keys": {"p256dh": keys.get("p256dh", ""), "auth": keys.get("auth", "")},
        "registered_at": __import__("app.utils.text", fromlist=["utcnow"]).utcnow().isoformat(),
    }
    device.endpoint_profile = profile
    return profile["web_push"]


def clear_web_subscription(device: Device) -> None:
    profile = dict(getattr(device, "endpoint_profile", None) or {})
    if "web_push" in profile:
        profile.pop("web_push", None)
        device.endpoint_profile = profile


def _send_sync(device: Device, *, title: str, body: str, url: str) -> str:
    from pywebpush import WebPushException, webpush

    sub = web_subscription(device)
    if sub is None:
        return "no_subscription"
    try:
        webpush(
            subscription_info={
                "endpoint": sub["endpoint"],
                "keys": dict(sub.get("keys") or {}),
            },
            data=json.dumps({"title": title, "body": body, "url": url}),
            vapid_private_key=(settings.web_push_vapid_private_key or "").strip(),
            vapid_claims={"sub": settings.web_push_vapid_subject or "mailto:owner@evie.local"},
        )
        return "sent"
    except WebPushException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (404, 410):
            clear_web_subscription(device)
            return "subscription_cleared"
        logger.warning("web push failed: %s", exc)
        return "failed"
    except Exception as exc:  # noqa: BLE001 - push must never break the caller
        logger.warning("web push failed: %s", exc)
        return "failed"


async def send_web_push(
    device: Device,
    *,
    title: str,
    body: str,
    url: str = "/evie/",
) -> str:
    """Fire-and-forget-friendly, non-blocking best-effort send."""

    if not vapid_configured() or web_subscription(device) is None:
        return "skipped"
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(
            None,
            lambda: _send_sync(device, title=title, body=body, url=url),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("web push task failed: %s", exc)
        return "failed"
