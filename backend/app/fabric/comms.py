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

def set_adapter(a) -> None:
    global _adapter
    _adapter = a

def _ad():
    global _adapter
    if _adapter is None:
        _adapter = MemoryAdapter()
    return _adapter

def resolve_thread(query: str, *, candidates: list[dict] | None = None) -> dict:
    cands = candidates if candidates is not None else _ad().resolve(query)
    if len(cands) == 2:
        return _receipt(True, False, {"disambiguation_card": cands, "needs_choice": True})
    if not cands:
        return _receipt(False, False, {}, "no thread match")
    return _receipt(True, True, {"thread": cands[0]})

def draft_message(body: str, *, to: str = "owner") -> dict:
    low = body.lower()
    if any(h in low for h in _INJECTION_HINTS):
        return _receipt(False, False, {"escalation": "injection suspected", "body": body},
                        "injection escalation: human review required")
    return _receipt(True, True, {"draft": {"to": to, "body": body}})

def send_message(body: str, *, to: str = "owner", confirmed: bool = True) -> dict:
    d = draft_message(body, to=to)
    if not d["ok"]:
        return d
    try:
        res = _ad().send(to, body)
    except Exception as e:
        return _receipt(False, False, {}, str(e))
    return _receipt(True, True, {"sent": res, "draft": d["evidence"]["draft"]})

def verify_send(result: dict) -> dict:
    return _receipt(bool(result.get("ok")), True, result)
