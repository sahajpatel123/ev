"""CLI entrypoint: run the biometric compliance retention sweep.

Bootstraps the schema first so the sweep is safe to run against a fresh
database (idempotent; uses the same ``create_all`` path as the eval gates).
"""

from __future__ import annotations

import asyncio

from app.workers.jobs import run_compliance_retention


def main() -> None:
    import json

    async def _bootstrap() -> None:
        from app.db import init_db

        await init_db()

    asyncio.run(_bootstrap())
    print(json.dumps(run_compliance_retention(), indent=2, default=str))


if __name__ == "__main__":
    main()
