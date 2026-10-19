"""User-facing claims come only from verified receipts."""
from __future__ import annotations

_SUCCESS = ("done.", "i created", "i sent", "i deleted", "successfully", "all set")


def claims_success(text: str) -> bool:
    low = (text or "").strip().lower()
    return any(mark in low for mark in _SUCCESS)


def claim(receipt: dict) -> str:
    """One sentence. Success only when every step was verified."""
    steps = list((receipt.get("evidence") or {}).get("steps") or [])
    errors = " ".join(str(s.get("error") or "") for s in steps)
    errors = f"{receipt.get('error') or ''} {errors}".lower()
    if "denied" in errors:
        return "I won't do that."
    if "confirm" in errors or any((s.get("evidence") or {}).get("needs_confirm") or s.get("confirm") for s in steps):
        return "That needs your confirmation before I do it."
    verified = bool(receipt.get("ok")) and bool(receipt.get("verified"))
    if steps:
        verified = verified and all(bool(s.get("verified")) and bool(s.get("ok")) for s in steps)
    if verified and steps:
        return "Done."
    if not steps and not receipt.get("ok"):
        detail = str(receipt.get("error") or "I can't do that yet.")
        return f"That didn't work. {detail}"
    detail = str(receipt.get("error") or "it was not verified")
    return f"That didn't work. {detail}"
