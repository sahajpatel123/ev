"""Database migration/schema parity (G1.1 observability).

The G1 pass discovered the live database silently THREE revisions behind its
own codebase — schema existed via create_all while alembic_version lagged.
This module makes that failure mode visible:

- EXPECTED_HEAD: head revision of the alembic script directory on disk
- ACTUAL_REVISION: the database's alembic_version row
- parity: YES / NO

OBSERVABILITY ONLY. Nothing here runs migrations automatically, and nothing
destructive. Mission Control / Self Diagnostics consume this read model.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_BACKEND_ROOT = Path(__file__).resolve().parents[2]


def expected_head() -> str | None:
    """Head revision of the migration scripts on disk.

    Multiple heads (a branched chain) is itself a parity failure → None.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    ini = _BACKEND_ROOT / "alembic.ini"
    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
    try:
        heads = ScriptDirectory.from_config(cfg).get_heads()
    except Exception:  # noqa: BLE001 - observability must not crash health
        return None
    return heads[0] if len(heads) == 1 else None


async def migration_parity(session: AsyncSession) -> dict:
    """Compare the DB's alembic_version against the script directory head."""
    exp = expected_head()
    try:
        row = (
            await session.execute(text("SELECT version_num FROM alembic_version"))
        ).scalars().first()
        actual: str | None = row if isinstance(row, str) else None
    except Exception:  # noqa: BLE001 - unmigrated/absent table is a real state
        actual = None
    parity = "YES" if (exp is not None and actual == exp) else "NO"
    return {
        "expected_head": exp,
        "actual_revision": actual,
        "parity": parity,
        "status": "ok" if parity == "YES" else "degraded",
    }
