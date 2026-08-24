"""P0 CONTAINMENT — destructive maintenance gate (incident 2026-08-25).

LAW: ordinary master authentication alone must never execute production
destructive operations (restore/wipe/reseed). Destructive maintenance
requires ALL of:

1. Explicit environment identity (EV_ENV). Production refuses destructive
   operations unless maintenance mode is enabled.
2. Maintenance mode enabled via EV_MAINTENANCE_MODE=1 in the process
   environment (set by a human operator before starting work).
3. A short-lived single-use confirmation token minted by
   ``prepare_destructive_operation`` bound to: operation name, target
   identifier (e.g. backup path), environment, and a database fingerprint.
4. The live database fingerprint captured at prepare time must still match
   at execute time (guards against racing concurrent mutations).

No tokens are printed in logs. Tokens expire. One token = one execution.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any

TOKEN_TTL_SECONDS = 300


class DestructiveOperationError(Exception):
    def __init__(self, code: str, message: str, status: int = 403):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)


def environment() -> str:
    """Explicit environment identity. Never inferred from hostname."""
    return os.environ.get("EV_ENV", "development").strip().lower()


def is_production() -> bool:
    return environment() == "production"


def maintenance_mode_enabled() -> bool:
    return os.environ.get("EV_MAINTENANCE_MODE", "").strip() == "1"


def database_fingerprint(session: Any) -> str:
    """Cheap stable fingerprint of live canonical state for race detection."""
    try:
        from sqlalchemy import text

        counts = {}
        for table in ("projects", "goals", "commitments", "events", "devices"):
            try:
                counts[table] = int(
                    session.execute(text(f"select count(*) from {table}")).scalar_one()
                )
            except Exception:
                counts[table] = -1
        raw = ",".join(f"{k}:{v}" for k, v in sorted(counts.items()))
        return hashlib.sha256(raw.encode()).hexdigest()[:16]
    except Exception:
        return "unavailable"


@dataclass(frozen=True)
class Confirmation:
    operation: str
    target: str
    environment: str
    fingerprint: str
    expires_at: float


_TOKENS: dict[str, Confirmation] = {}


def prepare_destructive_operation(
    session: Any,
    *,
    operation: str,
    target: str,
) -> dict[str, Any]:
    if not maintenance_mode_enabled():
        raise DestructiveOperationError(
            "MAINTENANCE_MODE_DISABLED",
            "Destructive maintenance requires EV_MAINTENANCE_MODE=1.",
        )
    fp = database_fingerprint(session)
    token = secrets.token_urlsafe(24)
    _TOKENS[token] = Confirmation(
        operation=operation,
        target=target,
        environment=environment(),
        fingerprint=fp,
        expires_at=time.time() + TOKEN_TTL_SECONDS,
    )
    # Opportunistic cleanup of expired tokens.
    now = time.time()
    for k in [k for k, v in _TOKENS.items() if v.expires_at < now]:
        _TOKENS.pop(k, None)
    return {
        "confirmation_token": token,
        "operation": operation,
        "environment": environment(),
        "database_fingerprint": fp,
        "expires_in_seconds": TOKEN_TTL_SECONDS,
        "single_use": True,
    }


def verify_destructive_confirmation(
    session: Any,
    *,
    token: str | None,
    operation: str,
    target: str,
) -> Confirmation:
    """Single-use verification. Raises DestructiveOperationError on any miss.

    Production additionally requires maintenance mode to STILL be enabled
    (mode may be flipped off between prepare and execute).
    """
    if not token:
        raise DestructiveOperationError(
            "CONFIRMATION_REQUIRED",
            "Destructive operations require a confirmation token from "
            "POST /backup/restore/prepare.",
        )
    conf = _TOKENS.pop(token, None)  # single-use: consume immediately
    if conf is None:
        raise DestructiveOperationError(
            "CONFIRMATION_INVALID_OR_USED",
            "Confirmation token unknown, expired, or already used.",
            status=409,
        )
    if conf.operation != operation or conf.target != target:
        raise DestructiveOperationError(
            "CONFIRMATION_MISMATCH",
            "Confirmation does not match this operation/target.",
            status=409,
        )
    if conf.environment != environment():
        raise DestructiveOperationError(
            "ENVIRONMENT_MISMATCH",
            "Confirmation was issued for a different environment.",
            status=409,
        )
    if conf.expires_at < time.time():
        raise DestructiveOperationError(
            "CONFIRMATION_EXPIRED",
            "Confirmation token expired; prepare again.",
            status=409,
        )
    if is_production() and not maintenance_mode_enabled():
        raise DestructiveOperationError(
            "MAINTENANCE_MODE_DISABLED",
            "Maintenance mode was disabled after preparation.",
        )
    # Race guard: live fingerprint must still match what the operator approved.
    fp_now = database_fingerprint(session)
    if fp_now != "unavailable" and conf.fingerprint != "unavailable" and fp_now != conf.fingerprint:
        raise DestructiveOperationError(
            "DATABASE_STATE_CHANGED",
            "Live database changed since confirmation; prepare again.",
            status=409,
        )
    return conf


def require_production_maintenance_gate(operation: str, target: str) -> None:
    """Fast-fail for production environments without full maintenance setup."""
    if not is_production():
        return
    if not maintenance_mode_enabled():
        raise DestructiveOperationError(
            "PRODUCTION_MAINTENANCE_DISABLED",
            "Production destructive operations require EV_MAINTENANCE_MODE=1.",
        )
    del operation, target
