"""Tests for ``make prune`` (LAUNCH disk governance)."""

from __future__ import annotations

from app.config import settings
from app.scripts import prune


def _touch(path, size: int = 100) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def test_backup_prune_keeps_newest_and_counts_bytes(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    backups = tmp_path / "backups"
    _touch(backups / "ev-backup-old.evbackup", size=1000)
    _touch(backups / "ev-backup-new.evbackup", size=2000)

    report = prune._prune_backups(keep=1, dry_run=True)
    assert report["bytes_freed"] == 1000
    assert report["items"] == ["ev-backup-old.evbackup"]
    assert (backups / "ev-backup-old.evbackup").exists()

    report = prune._prune_backups(keep=1, dry_run=False)
    assert report["bytes_freed"] == 1000
    assert not (backups / "ev-backup-old.evbackup").exists()
    assert (backups / "ev-backup-new.evbackup").exists()


def test_dev_cache_dry_run_does_not_delete(tmp_path, monkeypatch) -> None:
    cache = tmp_path / ".mypy_cache"
    _touch(cache / "a.pyc", size=500)
    monkeypatch.setattr(settings, "storage_root", str(tmp_path / "storage"))

    report = prune._prune_dev_caches(tmp_path, dry_run=True)
    assert report["bytes_freed"] == 500
    assert cache.exists()

    report = prune._prune_dev_caches(tmp_path, dry_run=False)
    assert report["bytes_freed"] == 500
    assert not cache.exists()
