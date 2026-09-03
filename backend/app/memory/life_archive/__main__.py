"""CLI: catalog (dry-run) or ingest a personal-data archive.

  cd backend && uv run python -m app.memory.life_archive catalog --root ~/personal-data-for-training
  cd backend && uv run python -m app.memory.life_archive ingest --root ~/personal-data-for-training --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path


def _preload_repo_env() -> None:
    """Load the repo-root .env when the CLI is launched from backend/."""
    repo_env = Path(__file__).resolve().parents[4] / ".env"
    if not repo_env.is_file():
        return
    for raw in repo_env.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


_preload_repo_env()

from app.memory.life_archive.catalog import catalog_tree, write_catalog  # noqa: E402
from app.memory.life_archive.classify import DEFAULT_ARCHIVE_ROOT  # noqa: E402
from app.memory.life_archive.ingest import ingest_records  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.memory.life_archive")
    sub = parser.add_subparsers(dest="cmd", required=True)

    catalog_p = sub.add_parser("catalog", help="Dry-run classify; write private summary")
    catalog_p.add_argument("--root", type=Path, default=DEFAULT_ARCHIVE_ROOT)

    ingest_p = sub.add_parser("ingest", help="Index/ingest classified files into Events")
    ingest_p.add_argument("--root", type=Path, default=DEFAULT_ARCHIVE_ROOT)
    ingest_p.add_argument("--apply", action="store_true", help="Write events (default is count-only)")
    ingest_p.add_argument(
        "--include",
        default="ingest,index",
        help="Comma dispositions to write: ingest,index",
    )
    ingest_p.add_argument(
        "--adapters",
        default="",
        help="Comma adapters to write (default: all classified ingest/index adapters)",
    )

    locate_p = sub.add_parser("locate", help="Rebuild shelf locator (no model context)")
    locate_p.set_defaults(rebuild=True)

    args = parser.parse_args()
    if args.cmd == "catalog":
        return _catalog(args.root)
    if args.cmd == "locate":
        return asyncio.run(_rebuild_locator())
    return asyncio.run(
        _ingest(args.root, apply=args.apply, include=args.include, adapters=args.adapters)
    )


def _catalog(root: Path) -> int:
    records = catalog_tree(root)
    summary = write_catalog(records, root=root)
    print(json.dumps(_public_summary(summary), indent=2, default=str))
    return 0


async def _ingest(root: Path, *, apply: bool, include: str, adapters: str = "") -> int:
    print("cataloging…", flush=True)
    records = catalog_tree(root)
    summary = write_catalog(records, root=root)
    wanted = frozenset(part.strip() for part in include.split(",") if part.strip())
    adapter_filter = frozenset(part.strip() for part in adapters.split(",") if part.strip())
    if adapter_filter:
        records = [row for row in records if row.adapter in adapter_filter]
    payload = {
        **_public_summary(summary),
        "apply": apply,
        "adapters": sorted(adapter_filter) if adapter_filter else ["all"],
        "selected_records": len(records),
        "db_dialect": _db_dialect(),
        "db_env_prefix": (os.environ.get("EV_DATABASE_URL") or "").split(":")[0] or "unset",
    }
    print(json.dumps({k: payload[k] for k in ("file_count", "selected_records", "adapters", "db_dialect", "apply")}, indent=2), flush=True)
    if not apply:
        payload["would_include"] = sorted(wanted)
        print(json.dumps(payload, indent=2))
        return 0
    from app.db import SessionLocal

    print("writing events…", flush=True)
    async with SessionLocal() as session:
        report = await ingest_records(session, root=root, records=records, include=wanted)
    payload["ingest"] = report
    print(json.dumps(payload, indent=2, default=str), flush=True)
    return 0


async def _rebuild_locator() -> int:
    from app.db import SessionLocal
    from app.memory.life_archive.locate import rebuild_locator

    async with SessionLocal() as session:
        payload = await rebuild_locator(session)
    print(json.dumps(payload, indent=2, default=str))
    return 0


def _db_dialect() -> str:
    from app.config import settings

    url = settings.database_url or ""
    return url.split(":", 1)[0] if url else "none"


def _public_summary(summary: dict) -> dict:
    return dict(summary)


if __name__ == "__main__":
    raise SystemExit(main())
