"""Bridge to the Swift ambient helper (Darwin-only; clean no-op elsewhere).

The helper is compiled once into a small cache directory and then invoked as
a short-lived subprocess per sample.  Everything here is fail-open in the
*degradation* sense: when Swift is missing, compilation fails, or the probe
times out, callers receive ``None`` and fall back to their text/env paths --
they never capture pixels, audio, or coordinates as a fallback.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

SOURCE = Path(__file__).parent / "swift" / "ambient_helper.swift"


def _cache_dir() -> Path:
    override = os.environ.get("EV_COLLECTOR_HELPER_DIR")
    if override:
        return Path(override)
    return Path.home() / ".ev" / "collector-helper"


def _prebuilt_binary() -> Path | None:
    override = os.environ.get("EV_COLLECTOR_HELPER_BIN")
    if override and Path(override).exists():
        return Path(override)
    return None


def helper_binary() -> Path | None:
    """Return a runnable helper binary, compiling it on first use."""

    if sys.platform != "darwin":
        return None
    prebuilt = _prebuilt_binary()
    if prebuilt is not None:
        return prebuilt
    if not SOURCE.exists():
        return None

    cache = _cache_dir()
    binary = cache / "ambient_helper"
    if binary.exists():
        return binary
    try:
        cache.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        # Keep the clang module cache inside our own directory so a read-only
        # home (~/.cache blocked) never prevents the helper from compiling.
        env["CLANG_MODULE_CACHE_PATH"] = str(cache / "clang-module-cache")
        result = subprocess.run(
            ["swiftc", "-O", "-o", str(binary), str(SOURCE)],
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not binary.exists():
        return None
    return binary


def run_helper(args: list[str], *, timeout: float = 8.0) -> dict | None:
    """Run one helper mode and return its JSON object, or ``None``."""

    binary = helper_binary()
    if binary is None:
        return None
    try:
        result = subprocess.run(
            [str(binary), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    line = (result.stdout or "").strip().splitlines()
    if not line:
        return None
    try:
        data = json.loads(line[0])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def run_capture_command(command: str, *, timeout: float = 15.0) -> bytes | None:
    """Run an explicit user-configured capture hook; returns raw bytes only
    when the user opted into a per-capture consent env var."""

    if not command:
        return None
    try:
        result = subprocess.run(
            shlex.split(command),
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout or None
