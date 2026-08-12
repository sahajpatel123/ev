"""Create an encrypted daily backup snapshot and prune old ones.

Usage:
    uv run python -m app.scripts.backup_snapshot \
        [--destination /path/to/backups] [--passphrase "$EV_BACKUP_PASSPHRASE"] \
        [--keep 7]

The passphrase must be set explicitly or via EV_BACKUP_PASSPHRASE; it is never
derived from the master key, so a leaked server key does not decrypt backups.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from app.config import settings
from app.db import SessionLocal
from app.services.backup import create_backup
from app.services.maintenance import prune_backups
from app.utils.text import utcnow


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", default=None, help="Backup directory or file path")
    parser.add_argument("--passphrase", default=None, help="Backup encryption passphrase")
    parser.add_argument("--keep", type=int, default=None, help="Backup files to retain")
    return parser


async def _run(args: argparse.Namespace) -> None:
    passphrase = args.passphrase or settings.backup_passphrase
    if not passphrase:
        raise SystemExit(
            "EV_BACKUP_PASSPHRASE (or --passphrase) is required; refusing to "
            "derive the backup key from the master key."
        )
    destination = args.destination
    backup_dir: Path | None = None
    if destination:
        path = Path(destination)
        if path.is_dir() or str(destination).endswith("/"):
            path.mkdir(parents=True, exist_ok=True)
            backup_dir = path
            stamp = utcnow().strftime("%Y%m%dT%H%M%S")
            destination = str(path / f"ev-backup-{stamp}.evbackup")
        else:
            backup_dir = path.parent
    else:
        backup_dir = Path(settings.storage_root) / "backups"
    async with SessionLocal() as session:
        result = await create_backup(
            session,
            passphrase=passphrase,
            destination=destination,
        )
    pruned = prune_backups(directory=str(backup_dir), keep=args.keep)
    print(
        json.dumps(
            {"backup": result, "pruned": pruned},
            sort_keys=True,
            default=str,
        )
    )


def main() -> None:
    asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    main()
