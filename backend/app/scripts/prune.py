"""``make prune`` — one-command disk recovery for an EV host.

Reclaims, in order:

1. old backups past ``EV_BACKUP_RETENTION_COUNT`` (default 7);
2. dev caches (``__pycache__``, pytest/mypy/ruff caches — they regenerate);
3. old model weights / expired dataset artifacts via the ML store (oldest
   first until the 5 GB disk guard is met; ``--all`` removes every artifact);
4. stale temp files under the storage root.

``--dry-run`` reports exactly what would be freed without deleting anything.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from app.config import settings
from app.ml.settings import get_ml_settings

DEV_CACHE_DIRS = (".pytest_cache", ".mypy_cache", ".ruff_cache")


def _dir_size(path: Path) -> int:
    return sum(
        file.stat().st_size
        for file in path.rglob("*")
        if file.is_file()
    )


def _pycache_dirs(root: Path) -> list[Path]:
    return [path for path in root.rglob("__pycache__") if path.is_dir()]


def _backup_candidates(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(
        directory.glob("ev-backup-*.evbackup"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def _prune_backups(*, keep: int, dry_run: bool) -> dict:
    directory = Path(settings.storage_root) / "backups"
    files = _backup_candidates(directory)
    removed = files[keep:]
    bytes_freed = sum(path.stat().st_size for path in removed)
    if removed and not dry_run:
        from app.services.maintenance import prune_backups

        prune_backups(directory=str(directory), keep=keep)
    return {
        "category": "backups",
        "items": [path.name for path in removed],
        "bytes_freed": bytes_freed,
    }


def _prune_dev_caches(backend_root: Path, *, dry_run: bool) -> dict:
    targets: list[Path] = []
    for name in DEV_CACHE_DIRS:
        candidate = backend_root / name
        if candidate.exists():
            targets.append(candidate)
    targets.extend(_pycache_dirs(backend_root))
    total = 0
    removed: list[str] = []
    for target in targets:
        size = _dir_size(target)
        total += size
        removed.append(str(target))
        if not dry_run:
            shutil.rmtree(target, ignore_errors=True)
    return {
        "category": "dev_caches",
        "items": removed,
        "bytes_freed": total,
    }


def _prune_ml(*, all_files: bool, dry_run: bool) -> dict:
    ml_settings = get_ml_settings()
    from app.datasets.store import prune_datasets
    from app.ml.store import prune_models

    models = prune_models(
        ml_settings,
        all_files=all_files,
        dry_run=dry_run,
    )
    datasets = prune_datasets(
        ml_settings,
        all_files=all_files,
        dry_run=dry_run,
    )
    model_bytes = sum(path.stat().st_size for path in models if path.is_file())
    dataset_bytes = sum(path.stat().st_size for path in datasets if path.is_file())
    return {
        "category": "ml_artifacts",
        "items": [str(path) for path in models + datasets],
        "bytes_freed": model_bytes + dataset_bytes,
        "models": [str(path) for path in models],
        "datasets": [str(path) for path in datasets],
    }


def _prune_storage_temp(storage_root: Path, *, dry_run: bool) -> dict:
    targets = [path for path in storage_root.rglob("*") if path.is_dir() and path.name in {"tmp", "temp", ".cache"}]
    total = 0
    removed: list[str] = []
    for target in targets:
        size = _dir_size(target)
        total += size
        removed.append(str(target))
        if not dry_run:
            shutil.rmtree(target, ignore_errors=True)
    return {
        "category": "storage_temp",
        "items": removed,
        "bytes_freed": total,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report only, delete nothing")
    parser.add_argument("--all", action="store_true", help="remove every cached ML artifact")
    parser.add_argument("--keep-backups", type=int, default=None, help="backups to retain")
    args = parser.parse_args()

    backend_root = Path(__file__).resolve().parents[2]
    storage_root = Path(settings.storage_root)
    keep = args.keep_backups if args.keep_backups is not None else settings.backup_retention_count

    categories = [
        _prune_backups(keep=keep, dry_run=args.dry_run),
        _prune_dev_caches(backend_root, dry_run=args.dry_run),
        _prune_ml(all_files=args.all, dry_run=args.dry_run),
        _prune_storage_temp(storage_root, dry_run=args.dry_run),
    ]
    total_bytes = sum(category["bytes_freed"] for category in categories)

    print("EV prune " + ("(dry run — nothing deleted)" if args.dry_run else ""))
    for category in categories:
        if category["bytes_freed"] or category["items"]:
            print(
                f"  {category['category']}: "
                f"{len(category['items'])} item(s), "
                f"{category['bytes_freed'] / (1024 * 1024):.1f} MB"
            )
            for item in category["items"][:20]:
                print(f"    {item}")
            if len(category["items"]) > 20:
                print(f"    ... and {len(category['items']) - 20} more")
    action = "would be freed" if args.dry_run else "freed"
    print(f"Total: {total_bytes / (1024 * 1024):.1f} MB {action}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
