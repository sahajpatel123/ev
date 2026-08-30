"""Runtime identity for request correlation (G2 PART 10).

One tiny source of truth for "which backend am I talking to" so the
physical PWA, gateway responses, and logs can always be correlated to the
exact running build. No secrets.
"""

from __future__ import annotations

import subprocess

_CACHED: str | None = None


def runtime_git_sha() -> str | None:
    global _CACHED
    if _CACHED is None:
        try:
            out = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            _CACHED = (out.stdout or "").strip() or None
        except Exception:
            _CACHED = None
    return _CACHED
