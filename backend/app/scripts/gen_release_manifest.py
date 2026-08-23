"""Generate clients/pwa/release.json — the ONE source of web release truth.

Run after any PWA build bump:

    make pwa-release-manifest

The manifest is read FROM DISK by hello/health at request time
(app.device_gateway.release), so the running backend always advertises exactly
what is served, even across a forgotten restart.

The build string itself still has one write-site (app.device_gateway.PWA_BUILD)
mirrored into frontend constants; tests/test_release_contract.py enforces that
every mirror equals this manifest, so a partial bump fails CI instead of
bricking phones.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from app.device_gateway import PROTOCOL_VERSION, PWA_BUILD
from app.device_gateway.release import (
    WEB_PROTOCOL,
    WEB_PROTOCOL_MAX,
    WEB_PROTOCOL_MIN,
    build_asset_manifest,
)


def main() -> int:
    assets = build_asset_manifest()
    manifest = {
        "web_build": PWA_BUILD,
        "web_protocol": WEB_PROTOCOL,
        "api_protocol": PROTOCOL_VERSION,
        "web_protocol_min": WEB_PROTOCOL_MIN,
        "web_protocol_max": WEB_PROTOCOL_MAX,
        "generated_at": datetime.now(UTC).isoformat(),
        **assets,
    }
    out = Path(__file__).resolve().parents[2] / "clients" / "pwa" / "release.json"
    out.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {out}")
    print(f"  web_build={manifest['web_build']} protocol={manifest['web_protocol']}")
    missing = [n for n, h in manifest["files"].items() if not h]
    if missing:
        print(f"  WARNING: missing assets (hash null): {', '.join(missing)}")
        return 1
    print(f"  asset_manifest_hash={manifest['asset_manifest_hash'][:16]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
