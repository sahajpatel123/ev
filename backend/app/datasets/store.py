"""Download/verify/prune for dataset artifacts."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urlparse

from app.datasets.registry import DatasetRegistry, DatasetRegistryError, DatasetSpec, guard_eval
from app.ml.registry import ChecksumError
from app.ml.settings import MLSettings
from app.ml.store import (
    RangeIgnoredError,
    disk_free_gb,
    ensure_disk_guard,
    open_source,
    sha256_file,
)


def dataset_cache_dir(settings: MLSettings) -> Path:
    path = settings.ml_dataset_dir
    path.mkdir(parents=True, exist_ok=True)
    return path


def dataset_target_path(settings: MLSettings, spec: DatasetSpec) -> Path:
    suffix = ".bin"
    if spec.source_url:
        suffix = Path(unquote(urlparse(spec.source_url).path)).suffix or ".bin"
    return dataset_cache_dir(settings) / f"{spec.name}{suffix}"


def download_dataset(spec: DatasetSpec, settings: MLSettings, *, stream: bool = False) -> Path:
    """Download a dataset atomically with checksum verification.

    ``stream`` is accepted for subset/streaming consumers; the download itself
    is always chunked. An unpinned sha256 is refused, as is a disk below the
    guard threshold.
    """

    if spec.source_url is None:
        raise DatasetRegistryError(f"dataset {spec.name!r} has no source_url to download")
    if not spec.sha256:
        raise DatasetRegistryError(
            f"dataset {spec.name!r} has no sha256 pin; seed manifests are not "
            "downloadable until a maintainer pins and verifies the checksum"
        )
    ensure_disk_guard(dataset_cache_dir(settings), settings.ml_min_free_gb)
    target = dataset_target_path(settings, spec)
    if target.exists() and sha256_file(target) == spec.sha256:
        return target
    partial = target.with_name(f"{target.name}.part")
    while True:
        offset = partial.stat().st_size if partial.exists() else 0
        try:
            with open_source(spec.source_url, offset=offset) as chunks, partial.open("ab") as out:
                for chunk in chunks:
                    out.write(chunk)
            actual = sha256_file(partial)
            if actual != spec.sha256:
                partial.unlink(missing_ok=True)
                raise ChecksumError(
                    f"sha256 mismatch for {spec.name}: expected {spec.sha256}, got {actual}; "
                    "artifact removed"
                )
            os.replace(partial, target)
            return target
        except RangeIgnoredError:
            partial.unlink(missing_ok=True)
            continue


def verify_dataset(spec: DatasetSpec, settings: MLSettings) -> bool:
    target = dataset_target_path(settings, spec)
    if not target.exists():
        return False
    actual = sha256_file(target)
    if spec.sha256 and actual != spec.sha256:
        target.unlink(missing_ok=True)
        raise ChecksumError(
            f"cached dataset {spec.name!r} is corrupt: expected {spec.sha256}, got {actual}; "
            "artifact removed"
        )
    return True


def prune_datasets(
    settings: MLSettings,
    *,
    all_files: bool = False,
    dry_run: bool = False,
    protected: set[str] | None = None,
) -> list[Path]:
    cache = settings.ml_dataset_dir
    if not cache.exists():
        return []
    protected = protected or set()
    artifacts = sorted(
        (path for path in cache.iterdir() if path.is_file() and path.name not in protected),
        key=lambda path: path.stat().st_mtime,
    )
    removed: list[Path] = []
    free = disk_free_gb(cache)
    while artifacts and (all_files or free < settings.ml_min_free_gb):
        artifact = artifacts.pop(0)
        if not dry_run:
            artifact.unlink(missing_ok=True)
            free = disk_free_gb(cache)
        removed.append(artifact)
    return removed


@contextmanager
def use_dataset(
    name: str,
    registry: DatasetRegistry,
    settings: MLSettings,
    *,
    eval_context: bool = False,
    delete_after_use: bool = True,
    stream: bool = False,
) -> Iterator[Path]:
    """Download (or reuse) a dataset, then delete it after use by default."""

    spec = registry.get(name)
    guard_eval(spec, eval_context=eval_context)
    path = download_dataset(spec, settings, stream=stream)
    try:
        yield path
    finally:
        if delete_after_use:
            path.unlink(missing_ok=True)
