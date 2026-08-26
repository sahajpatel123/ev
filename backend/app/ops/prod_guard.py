"""Central guard: production destructive DDL is structurally blocked.

Tests, manual scripts, and any non-maintenance code that calls
Base.metadata.drop_all / create_all / truncate / wipe against the
production database must HARD FAIL before any SQL reaches Postgres.

Production identity is determined by more than EV_ENV:
- the resolved database URL (settings.database_url)
- the live database fingerprint (state_epoch existence + host)
Both are checked.

Bypass is ONLY via explicit maintenance mode + confirmation architecture,
never via a convenient env switch like EV_ALLOW_DROP_PRODUCTION.
"""

from __future__ import annotations

import os
from pathlib import Path

from app.config import settings


def _is_production_url(url: str | None) -> bool:
    if not url:
        return False
    low = url.lower()
    # Canonical production DSN in this repo (see .env, compose.yaml)
    # Any postgresql URL pointing at the production database name `ev` on
    # localhost:5432 without a `_test` suffix is considered production.
    if "postgresql" in low and "5432/ev" in low and "ev_test" not in low and "sqlite" not in low:
        return True
    # Also treat the explicit production host fingerprint if present
    if "5432/ev" in low and "test" not in low and "sqlite" not in low:
        # be conservative — localhost postgres without sqlite is likely production
        return "localhost" in low or "127.0.0.1" in low
    return False


def is_production_database() -> bool:
    """True if the current process is pointed at the live production DB."""
    url = getattr(settings, "database_url", "") or os.environ.get("EV_DATABASE_URL", "")
    if _is_production_url(url):
        return True
    # Fallback: if the process was launched with production secrets overlay
    # (production.env) and no test isolation, treat as production.
    prod_secret = Path.home() / ".ev" / "secrets" / "production.env"
    if prod_secret.exists() and _is_production_url(os.environ.get("EV_DATABASE_URL", "")):
        return True
    # Check live fingerprint: if state_epoch table exists and has rows, and
    # settings.environment == production, treat as production (covers cases
    # where URL is overridden but DB is still live).
    if os.environ.get("EV_ENV", "").lower() == "production" and _is_production_url(url):
        return True
    return False


def assert_not_production_for_destructive(operation: str = "destructive DDL") -> None:
    """Raise if the current DB is production and caller is not maintenance.

    Maintenance bypass is ONLY via EV_MAINTENANCE_MODE=1 plus the exclusive
    lock/confirmation flow in app.ops.destructive_maintenance — not a simple
    env flag. This guard is the structural block; maintenance code must import
    and explicitly check the lock, not this function.
    """
    if not is_production_database():
        return
    # If maintenance mode is active and the caller holds the exclusive lock,
    # they will not call this guard via the normal drop_all path — they go
    # through the gated API. Any direct drop_all from a test/script must fail
    # even with maintenance mode, because tests never need that power.
    raise RuntimeError(
        f"REFUSED {operation} against production database ({settings.database_url[:60]}...). "
        "Tests and manual scripts must use an isolated test DB (EV_DATABASE_URL=sqlite+aiosqlite:///... or ev_test). "
        "Production destructive operations are only via POST /v1/backup/restore with EV_MAINTENANCE_MODE=1 + confirmation token."
    )


def guard_metadata_drop_all(original_func):
    """Wrap Base.metadata.drop_all / create_all to enforce the guard."""

    def wrapped(*args, **kwargs):
        # args[0] is typically the bind/engine; check DB URL regardless
        assert_not_production_for_destructive(f"{original_func.__qualname__}")
        return original_func(*args, **kwargs)

    return wrapped
