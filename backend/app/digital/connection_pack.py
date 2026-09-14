"""ONE owner connection pack — every remaining auth action in one place."""

from __future__ import annotations

from typing import Any

from app.config import settings


def connection_pack() -> dict[str, Any]:
    google_client = bool(getattr(settings, "google_oauth_client_id", None) and getattr(settings, "google_oauth_client_secret", None))
    steps: list[dict[str, Any]] = []
    if not google_client:
        steps.append(
            {
                "service": "Google Cloud OAuth client (once)",
                "why": "Evie uses official Google OAuth. There is no Google client ID on this machine yet, so Connect cannot start.",
                "ui_steps": [
                    "In Google Cloud Console, create (or reuse) an OAuth client of type Desktop or Web.",
                    "Authorized redirect: the existing EV Google redirect URI from docs/INTEGRATIONS.md / Home Station Integrations.",
                    "Put the client id and secret in the owner env as EV_GOOGLE_OAUTH_CLIENT_ID and EV_GOOGLE_OAUTH_CLIENT_SECRET.",
                    "Restart the API after saving. Do not paste the secret into Evie chat.",
                ],
                "scopes": [],
                "do_not_share": ["client secret", "any Google password"],
                "ready": False,
            }
        )
    steps.append(
        {
            "service": "Google (Gmail + Contacts + Calendar)",
            "why": "Native Gmail/Contacts/Calendar need an OAuth grant. Evie never asks for your Google password.",
            "ui_steps": [
                "Open Home Station → Integrations → Google / Mail.",
                "Click Connect / Authorize.",
                "Sign in with the owner Google account in the normal Google screen.",
                "Approve only the listed scopes. Incremental consent may appear later for send/modify.",
            ],
            "scopes": [
                "openid",
                "email",
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/gmail.modify",
                "https://www.googleapis.com/auth/gmail.send",
                "https://www.googleapis.com/auth/gmail.compose",
                "https://www.googleapis.com/auth/contacts",
                "https://www.googleapis.com/auth/calendar.readonly",
                "https://www.googleapis.com/auth/calendar.events",
            ],
            "do_not_share": [
                "Google password",
                "OAuth refresh or access tokens",
                "app passwords",
                "2FA codes in chat",
            ],
            "ready": google_client,
            "note": "If EV_GOOGLE_OAUTH_CLIENT_ID is unset, register a Google Cloud OAuth client first (see docs/INTEGRATIONS.md) then repeat Connect.",
        }
    )
    steps.append(
        {
            "service": "WhatsApp Web (Home Station)",
            "why": "There is no official personal WhatsApp inbox API. Evie operates the owner's legitimately linked WhatsApp Web session on the Mac.",
            "ui_steps": [
                "On the Home Station Mac, open WhatsApp Web in the Evie service browser profile (or the linked Chrome profile Evie uses).",
                "On your phone, open WhatsApp → Linked devices → Link a device.",
                "Scan the QR code shown on the Mac.",
                "Keep that session linked. Do not paste cookies or QR screenshots into chat.",
            ],
            "scopes": ["WhatsApp Web session cookie (browser-isolated, never sent to Muse)"],
            "do_not_share": [
                "WhatsApp password (there isn't one to give Evie)",
                "session cookies",
                "QR code images in Evie chat",
            ],
            "ready": False,
        }
    )
    return {
        "title": "OWNER CONNECTION PACK",
        "count": len(steps),
        "steps": steps,
        "new_physical_hardware": False,
        "xcode_required": False,
        "passwords_in_chat": "NEVER",
    }
