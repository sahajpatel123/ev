"""Comms lane: thread resolve->draft->confirm->send->verify; disambiguation; injection escalation; fake-able adapters."""
from __future__ import annotations

from typing import Any

_INJECTION_HINTS = ("ignore previous", "system:", "override", "exfiltrate", "send password")

def _receipt(ok, verified, evidence=None, error=None) -> dict:
    return {"ok": ok, "verified": verified, "evidence": evidence or {}, "error": error, "undo": None}

class MemoryAdapter:
    def __init__(self): self.sent = []
    def resolve(self, query: str) -> list[dict]:
        return [{"id": "owner", "name": "owner"}]
    def send(self, to: str, body: str) -> dict:
        self.sent.append({"to": to, "body": body})
        return {"id": str(len(self.sent))}

_adapter: Any = None
_SENT_KEYS: dict[str, dict] = {}


def set_adapter(a) -> None:
    global _adapter
    _adapter = a
    _SENT_KEYS.clear()

def _ad():
    global _adapter
    if _adapter is None:
        _adapter = MemoryAdapter()
    return _adapter

def resolve_thread(query: str, *, candidates: list[dict] | None = None) -> dict:
    cands = candidates if candidates is not None else _ad().resolve(query)
    if not cands:
        return _receipt(False, False, {}, "no thread match")
    if len(cands) != 1:
        return _receipt(True, False, {"disambiguation_card": cands, "needs_choice": True})
    return _receipt(True, True, {"thread": cands[0]})

def draft_message(body: str, *, to: str = "owner") -> dict:
    low = body.lower()
    if any(h in low for h in _INJECTION_HINTS):
        return _receipt(False, False, {"escalation": "injection suspected", "body": body},
                        "injection escalation: human review required")
    return _receipt(True, True, {"draft": {"to": to, "body": body}})

def send_message(body: str, *, to: str = "owner", confirmed: bool = False,
                 key: str | None = None) -> dict:
    # D9: draft -> CONFIRM -> send -> verify. Unconfirmed sends park.
    d = draft_message(body, to=to)
    if not d["ok"]:
        return d
    if not confirmed:
        return _receipt(False, False, {"needs_confirm": True, "draft": d["evidence"]["draft"]},
                        "confirm required before send")
    if key is not None and key in _SENT_KEYS:
        prior = _SENT_KEYS[key]
        return _receipt(True, True, {"sent": prior, "duplicate": True, "draft": d["evidence"]["draft"]})
    try:
        res = _ad().send(to, body)
    except Exception as e:
        return _receipt(False, False, {}, str(e))
    if key is not None:
        _SENT_KEYS[key] = res
    return _receipt(True, True, {"sent": res, "draft": d["evidence"]["draft"]})

def verify_send(result: dict) -> dict:
    return _receipt(bool(result.get("ok")), True, result)


# --- v2: channel-aware sends with standing grants (WP8) ---

import re as _re

CHANNELS = ("messages", "imessage", "sms", "whatsapp", "mail", "gmail")

_MONEY_RE = _re.compile(
    r"(?i)(\$|USD|EUR|GBP|¥)\s?\d|payment|invoice|wire transfer|venmo|zelle|"
    r"paypal|bank account|routing number|sort code|send money|pay you")
_LEGAL_RE = _re.compile(
    r"(?i)\b(contract|lawsuit|attorney|lawyer|settlement|subpoena|court order|"
    r"legal action|cease and desist|\bNDA\b)")
_CREDENTIAL_RE = _re.compile(
    r"(?i)(api[_-]?key|token|secret|password|passwd|bearer)\s*[:=]\s*\S+|"
    r"\b(password|passwd|private key|secret key)\b")


def normalize_channel(channel: str) -> str:
    c = (channel or "").strip().lower()
    if c in {"imessage", "sms", "message", "messages", "text"}:
        return "messages"
    if c in {"wa", "whatsapp"}:
        return "whatsapp"
    if c in {"email", "e-mail", "mail", "gmail", "applemail", "apple_mail"}:
        return c if c in {"gmail", "mail"} else "mail"
    return ""


def content_flags(body: str, subject: str = "") -> dict[str, bool]:
    """D8 never-grantable content classes (documented heuristics, fail-open
    to human review: a flag only disables standing grants, never sends)."""
    text = f"{subject or ''}\n{body or ''}"
    return {
        "money": bool(_MONEY_RE.search(text)),
        "legal": bool(_LEGAL_RE.search(text)),
        "credential": bool(_CREDENTIAL_RE.search(text)),
    }


def grant_scope(channel: str, recipient: str) -> str:
    return f"comms:{normalize_channel(channel)}:{(recipient or '').strip().lower()}"


def grant_usable(channel: str, recipient: str, flags: dict[str, bool],
                 first_contact: bool | None) -> tuple[bool, str]:
    """A standing grant covers this send only when active, consented scope,
    no never-grantable content, and not a first contact."""
    from .grants import check_grant

    if not recipient or not recipient.strip():
        return False, "no recipient"
    if flags.get("money") or flags.get("legal") or flags.get("credential"):
        return False, "never-grantable content class"
    if first_contact is not False:
        return False, "first contact (or unknown history)"
    scope = grant_scope(channel, recipient)
    held = check_grant(scope)
    if not held.get("ok"):
        return False, "no active standing grant"
    return True, scope
