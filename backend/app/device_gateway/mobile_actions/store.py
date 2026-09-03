"""In-memory mobile action authority. Home Station process is the single store.

Tokens are high-entropy, short-lived, device-bound. No long-lived Evie
credentials are stored in the Shortcut.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from typing import Any

from . import ACTION_TTL_S, BRIDGE_PROTOCOL, BRIDGE_VERSION, RESOLVE_RETRY_S

_LOCK = threading.Lock()
_ACTIONS: dict[str, dict[str, Any]] = {}
_ACTION_TOKENS: dict[str, str] = {}
_COMPLETION_TOKENS: dict[str, str] = {}
_DOWNLOAD_TOKENS: dict[str, dict[str, Any]] = {}
_HANDSHAKES: dict[str, dict[str, Any]] = {}


def reset_for_tests() -> None:
    with _LOCK:
        _ACTIONS.clear()
        _ACTION_TOKENS.clear()
        _COMPLETION_TOKENS.clear()
        _DOWNLOAD_TOKENS.clear()
        _HANDSHAKES.clear()


def _gc_locked(now: float) -> None:
    expired: list[str] = []
    for action_id, row in _ACTIONS.items():
        if float(row.get("exp") or 0) < now and row.get("state") not in {
            "executed",
            "failed",
            "cancelled",
            "expired",
        }:
            row["state"] = "expired"
            row["failure"] = "EXPIRED"
            expired.append(action_id)
        elif float(row.get("exp") or 0) < now - 600:
            expired.append(action_id)
    for action_id in expired:
        expired_row = _ACTIONS.get(action_id)
        if expired_row is None:
            continue
        _ACTION_TOKENS.pop(str(expired_row.get("action_token") or ""), None)
        _COMPLETION_TOKENS.pop(str(expired_row.get("completion_token") or ""), None)
        if expired_row.get("state") == "expired" and float(expired_row.get("exp") or 0) < now - 600:
            _ACTIONS.pop(action_id, None)
    for token, meta in list(_DOWNLOAD_TOKENS.items()):
        if float(meta.get("exp") or 0) < now:
            _DOWNLOAD_TOKENS.pop(token, None)


def _new_secret(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def put_handshake(device_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    native = bool(payload.get("native_shell") or payload.get("native_broker"))
    os_version = str(payload.get("os_version") or "")[:32]
    broker_version = str(payload.get("broker_version") or payload.get("native_broker_version") or "")[:32]
    row: dict[str, Any] = {
        "device_id": device_id,
        "native_shell": native,
        "broker_version": broker_version,
        "os_version": os_version,
        "permissions": payload.get("permissions") if isinstance(payload.get("permissions"), dict) else {},
        "legacy_bridge": bool(payload.get("legacy_bridge")),
        "bridge_installed": bool(payload.get("bridge_installed")) and bool(payload.get("legacy_bridge")),
        "bridge_version": str(payload.get("bridge_version") or "")[:32],
        "protocol": int(payload.get("protocol") or BRIDGE_PROTOCOL),
        "timezone": str(payload.get("timezone") or "")[:64],
        "locale": str(payload.get("locale") or "")[:32],
        "capabilities": [
            str(item)[:64]
            for item in (payload.get("capabilities") or [])
            if str(item).strip()
        ][:40],
        "updated_at": time.time(),
        "instance_id": str(payload.get("instance_id") or "")[:64],
        "compatible": int(payload.get("protocol") or BRIDGE_PROTOCOL) == BRIDGE_PROTOCOL,
    }
    with _LOCK:
        _HANDSHAKES[device_id] = row
        return dict(row)


def handshake_of(device_id: str) -> dict[str, Any]:
    with _LOCK:
        row = _HANDSHAKES.get(device_id)
        return dict(row) if row else {}


def create_action(record: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    action_id = "ma_" + secrets.token_urlsafe(12)
    action_token = _new_secret("mat_")
    completion_token = _new_secret("mac_")
    row = {
        **record,
        "action_id": action_id,
        "action_token": action_token,
        "completion_token": completion_token,
        "state": record.get("state") or "created",
        "created_at": now,
        "exp": now + float(record.get("ttl_s") or ACTION_TTL_S),
        "resolved_at": None,
        "claimed": False,
        "completed": False,
        "receipt": None,
        "authorized_run": None,
        "failure": None,
    }
    with _LOCK:
        _gc_locked(now)
        _ACTIONS[action_id] = row
        _ACTION_TOKENS[action_token] = action_id
        _COMPLETION_TOKENS[completion_token] = action_id
        return dict(row)


def restore_action(record: dict[str, Any]) -> dict[str, Any]:
    row = dict(record)
    action_id = str(row.get("action_id") or "")
    if not action_id:
        return row
    with _LOCK:
        _ACTIONS[action_id] = row
        token = str(row.get("action_token") or "")
        completion = str(row.get("completion_token") or "")
        if token:
            _ACTION_TOKENS[token] = action_id
        if completion:
            _COMPLETION_TOKENS[completion] = action_id
        return dict(row)


def get_action(action_id: str) -> dict[str, Any] | None:
    with _LOCK:
        _gc_locked(time.time())
        row = _ACTIONS.get(action_id)
        return dict(row) if row else None


def update_action(action_id: str, **fields: Any) -> dict[str, Any] | None:
    with _LOCK:
        row = _ACTIONS.get(action_id)
        if row is None:
            return None
        row.update(fields)
        return dict(row)


def action_by_token(token: str) -> dict[str, Any] | None:
    now = time.time()
    with _LOCK:
        _gc_locked(now)
        action_id = _ACTION_TOKENS.get(token or "")
        if not action_id:
            return None
        row = _ACTIONS.get(action_id)
        return dict(row) if row else None


def action_by_completion(token: str) -> dict[str, Any] | None:
    now = time.time()
    with _LOCK:
        _gc_locked(now)
        action_id = _COMPLETION_TOKENS.get(token or "")
        if not action_id:
            return None
        row = _ACTIONS.get(action_id)
        return dict(row) if row else None


def consume_action_token(token: str) -> None:
    with _LOCK:
        _ACTION_TOKENS.pop(token or "", None)


def consume_completion_token(token: str) -> None:
    with _LOCK:
        _COMPLETION_TOKENS.pop(token or "", None)


def resolve_window_ok(row: dict[str, Any], now: float | None = None) -> bool:
    clock = now if now is not None else time.time()
    if float(row.get("exp") or 0) < clock:
        return False
    resolved = row.get("resolved_at")
    if resolved is None:
        return True
    return (clock - float(resolved)) <= RESOLVE_RETRY_S and not row.get("claimed")


def mint_download_token(*, device_id: str, origin: str) -> str:
    token = secrets.token_urlsafe(18)
    with _LOCK:
        _DOWNLOAD_TOKENS[token] = {
            "device_id": device_id,
            "origin": origin,
            "exp": time.time() + 120,
        }
        return token


def consume_download_token(token: str) -> dict[str, Any] | None:
    with _LOCK:
        row = _DOWNLOAD_TOKENS.pop(token or "", None)
        if row is None or float(row.get("exp") or 0) < time.time():
            return None
        return dict(row)


def pending_confirmation(device_id: str) -> dict[str, Any] | None:
    now = time.time()
    with _LOCK:
        _gc_locked(now)
        owned = [
            row
            for row in _ACTIONS.values()
            if str(row.get("device_id")) == device_id
            and row.get("state") in {"awaiting_confirmation", "draft"}
        ]
        if not owned:
            return None
        owned.sort(key=lambda item: float(item.get("created_at") or 0), reverse=True)
        return dict(owned[0])


def last_for_device(device_id: str) -> dict[str, Any] | None:
    with _LOCK:
        owned = [row for row in _ACTIONS.values() if str(row.get("device_id")) == device_id]
        if not owned:
            return None
        owned.sort(key=lambda item: float(item.get("created_at") or 0), reverse=True)
        row = owned[0]
        return {
            "action_id": row.get("action_id"),
            "operation": row.get("operation"),
            "state": row.get("state"),
            "result": (row.get("receipt") or {}).get("result"),
        }


def capability_hash(handshake: dict[str, Any]) -> str:
    blob = ",".join(sorted(handshake.get("capabilities") or [])).encode()
    return hashlib.sha256(blob).hexdigest()[:12] if blob else "none"


def public_row(row: dict[str, Any]) -> dict[str, Any]:
    """Never include tokens or raw contact databases."""

    receipt_raw = row.get("receipt")
    receipt: dict[str, Any] = receipt_raw if isinstance(receipt_raw, dict) else {}
    return {
        "action_id": row.get("action_id"),
        "target_device": row.get("device_id"),
        "operation": row.get("operation"),
        "state": row.get("state"),
        "accepted": row.get("state")
        in {"authorized", "resolved", "executing", "executed", "awaiting_confirmation"},
        "executed": row.get("state") == "executed",
        "verified": bool(receipt.get("verified")),
        "requires_user_interaction": bool(row.get("requires_user_interaction")),
        "result": receipt.get("result") or row.get("result"),
        "failure": row.get("failure") or receipt.get("failure"),
        "confirmation_required": row.get("state") == "awaiting_confirmation",
        "confirmation_id": row.get("confirmation_id"),
        "class": row.get("class_level"),
        "risk_class": row.get("risk_class"),
        "method": row.get("method"),
        "verification_quality": row.get("verification_quality"),
        "system_ui_presented": bool((row.get("receipt") or {}).get("system_ui_presented")),
        "native_broker_version": row.get("broker_version") or BRIDGE_VERSION,
        "protocol": BRIDGE_PROTOCOL,
    }
