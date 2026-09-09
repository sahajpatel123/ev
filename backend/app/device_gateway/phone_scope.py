"""Phone-only change guardrails.

This module does not edit or import the frozen Mac live-voice implementation.
It gives phone-focused checks a single forbidden-path list and a deterministic
fingerprint helper that CI or a pre-commit wrapper can compare to a baseline.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable

FROZEN_MAC_LIVE_SURFACES = frozenset(
    {
        "backend/app/voice/live/grok_voice.py",
        "backend/app/voice/live/session.py",
        "backend/app/voice/live/transport.py",
        "macos/Sources/EV/TTSPlayer.swift",
        "macos/Sources/EV/LiveConversation.swift",
        "ios/EVClient/Sources/EVClient/LiveVoice.swift",
    }
)


def phone_scope_violations(paths: Iterable[str]) -> tuple[str, ...]:
    """Return changed paths that are outside the iPhone-only work scope."""

    normalized = {
        str(path).replace("\\", "/").lstrip("./")
        for path in paths
        if str(path).strip()
    }
    return tuple(sorted(normalized & FROZEN_MAC_LIVE_SURFACES))


def frozen_surface_fingerprints(root: str | Path) -> dict[str, str]:
    """Hash frozen surfaces without loading their runtime dependencies."""

    base = Path(root)
    result: dict[str, str] = {}
    for relative in sorted(FROZEN_MAC_LIVE_SURFACES):
        path = base / relative
        if not path.is_file():
            result[relative] = "MISSING"
            continue
        result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def fingerprint_mismatches(
    actual: dict[str, str],
    expected: dict[str, str],
) -> dict[str, tuple[str | None, str | None]]:
    """Compare a recorded frozen-surface baseline without exposing contents."""

    mismatches: dict[str, tuple[str | None, str | None]] = {}
    for path in sorted(set(actual) | set(expected)):
        got = actual.get(path)
        want = expected.get(path)
        if got != want:
            mismatches[path] = (want, got)
    return mismatches
