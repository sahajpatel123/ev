"""Checksum-verified, atomic, resumable downloads plus the disk guard."""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from app.ml.registry import ChecksumError, DiskGuardError, ModelSpec, RegistryError
from app.ml.settings import MLSettings

CHUNK_SIZE = 1024 * 1024
DOWNLOAD_TIMEOUT = 60.0


class RangeIgnoredError(RuntimeError):
    """Server answered 200 instead of 206 to a Range request."""


def cache_dir(settings: MLSettings) -> Path:
    path = settings.ml_model_dir
    path.mkdir(parents=True, exist_ok=True)
    return path


def disk_free_gb(path: Path) -> float:
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    return shutil.disk_usage(probe).free / (1024**3)


def ensure_disk_guard(path: Path, min_free_gb: float) -> None:
    free = disk_free_gb(path)
    if free < min_free_gb:
        raise DiskGuardError(
            f"refusing download: free disk {free:.2f}GB < EV_ML_MIN_FREE_GB={min_free_gb:.2f}GB"
        )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def target_path(settings: MLSettings, spec: ModelSpec) -> Path:
    suffix = ".bin"
    if spec.source_url:
        suffix = Path(unquote(urlparse(spec.source_url).path)).suffix or ".bin"
    return cache_dir(settings) / f"{spec.name}{suffix}"


@contextmanager
def open_source(source_url: str, *, offset: int = 0) -> Iterator[Iterator[bytes]]:
    """Yield a chunked byte iterator for a file:// or http(s):// source."""

    parsed = urlparse(source_url)
    if parsed.scheme == "file":
        path = Path(unquote(parsed.path))

        def file_chunks() -> Iterator[bytes]:
            with path.open("rb") as handle:
                handle.seek(offset)
                while chunk := handle.read(CHUNK_SIZE):
                    yield chunk

        yield file_chunks()
        return
    if parsed.scheme not in ("http", "https"):
        raise RegistryError(f"unsupported source_url scheme: {parsed.scheme!r}")
    headers = {"Range": f"bytes={offset}-"} if offset else {}
    with httpx.stream(
        "GET",
        source_url,
        headers=headers,
        follow_redirects=True,
        timeout=DOWNLOAD_TIMEOUT,
    ) as response:
        response.raise_for_status()
        if offset and response.status_code == 200:
            raise RangeIgnoredError("server ignored Range; restarting download")
        yield response.iter_bytes(chunk_size=CHUNK_SIZE)


def download_model(spec: ModelSpec, settings: MLSettings) -> Path:
    """Download a model atomically and verify its sha256 pin.

    A partially downloaded file is resumed via Range. On checksum mismatch the
    artifact is removed and ``ChecksumError`` raised. The final file only ever
    appears under its canonical name after the full file verifies.
    """

    if spec.source_url is None:
        raise RegistryError(f"model {spec.name!r} has no source_url to download")
    if not spec.sha256:
        raise RegistryError(
            f"model {spec.name!r} has no sha256 pin; seed entries are not downloadable "
            "until a maintainer pins and verifies the checksum"
        )
    ensure_disk_guard(cache_dir(settings), settings.ml_min_free_gb)
    target = target_path(settings, spec)
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


def verify_model(spec: ModelSpec, settings: MLSettings) -> bool:
    """Verify a cached model's checksum; remove the artifact on mismatch."""

    target = target_path(settings, spec)
    if not target.exists():
        return False
    actual = sha256_file(target)
    if spec.sha256 and actual != spec.sha256:
        target.unlink(missing_ok=True)
        raise ChecksumError(
            f"cached model {spec.name!r} is corrupt: expected {spec.sha256}, got {actual}; "
            "artifact removed"
        )
    return True


def prune_models(
    settings: MLSettings,
    *,
    all_files: bool = False,
    dry_run: bool = False,
    protected: set[str] | None = None,
) -> list[Path]:
    """Evict least-recently-used weight files from the model cache.

    Default mode removes oldest files until at least ``EV_ML_MIN_FREE_GB`` is
    free; ``all_files=True`` removes every cached artifact.
    """

    cache = settings.ml_model_dir
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
