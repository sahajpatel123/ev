"""Channel-native recipient evidence.

Apple Contacts is not the address book of WhatsApp. A person who exists in
the WhatsApp chat list is a real recipient for a WhatsApp send even when
they were never saved on this Mac, and blocking that send with "add them to
Contacts" is wrong. This module answers, per channel, "does this person
exist here?" from that channel's own data:

- ``whatsapp``: the WhatsApp chat list (open Web tab, then WhatsApp Desktop
  ChatStorage), independently of Apple Contacts.
- ``mail``: an email address is its own proof.
- other channels: no local native store yet, so ``None`` and the existing
  contact bridge decides.

The result carries a channel reference (``id``) so the life policy treats a
channel-native recipient as known without pretending they are in Contacts.
"""

from __future__ import annotations

from typing import Any

from app.ev.messaging.channels import normalize_channel


def _contact_from_whatsapp(match: dict[str, Any]) -> dict[str, Any] | None:
    status = str(match.get("status") or "none")
    if status == "ambiguous":
        return {
            "status": "ambiguous",
            "display": str(match.get("display") or ""),
            "candidates": list(match.get("candidates") or []),
        }
    if status not in {"unique", "desktop_only"}:
        return None
    peer_raw = match.get("peer")
    peer: dict[str, Any] = peer_raw if isinstance(peer_raw, dict) else {}
    return {
        "status": "unique",
        "id": str(match.get("chat_ref") or peer.get("jid") or peer.get("handle") or ""),
        "display": str(match.get("display") or peer.get("handle") or ""),
        "phone": str(peer.get("phone") or ""),
        "source": "whatsapp_web" if status == "unique" else "whatsapp_desktop",
    }


async def resolve_native_contact(channel: str | None, to: str) -> dict[str, Any] | None:
    """Channel-native proof of a recipient, or None.

    ``{"status": "unique", "id", "display", "phone", "source"}`` on success;
    ``{"status": "ambiguous", "candidates"}`` when the channel itself has
    several matches; None when the channel has no native store or no match.
    """

    who = (to or "").strip()
    if not who:
        return None
    canonical = normalize_channel(channel) or (channel or "").strip().lower()
    if canonical == "whatsapp":
        from app.ev.messaging import whatsapp_web

        return _contact_from_whatsapp(await whatsapp_web.resolve(who))
    if canonical == "mail" and "@" in who:
        return {"status": "unique", "id": who, "display": who, "source": "mail_address"}
    return None
