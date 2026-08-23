"""Promote the current canary artifact to stable (owner-approved builds only).

Usage:
    uv run python -m app.scripts.promote_stable [expected_native_build]

Promotes the EXACT canary artifact — no rebuild — after the owner has
physically verified it on the Primary iPhone (directive B20/B22).
"""

from __future__ import annotations

import sys

from app.device_gateway.release_portal import promote_canary_to_stable


def main() -> int:
    expected = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        published = promote_canary_to_stable(expected)
    except ValueError as exc:
        print(f"REFUSED: {exc}")
        print("Build the canary first, verify it physically, then promote.")
        return 1
    print(
        "STABLE promoted:"
        f" build {published.get('native_build')}"
        f" commit {str(published.get('commit'))[:12]}"
        f" sha256 {str(published.get('ipa_sha256'))[:16]}…"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
