"""Home Station setup check. Does not mutate tailnet ACLs or enable Funnel."""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def main() -> int:
    from app.device_gateway.health import snapshot
    from app.device_gateway.tailscale import maybe_apply_serve, probe

    print("== Evie Home Station ==")
    print("API bind expected: 127.0.0.1:8000 (launchd/ev.api.plist KeepAlive).")
    print("Install auto-start with ./launchd/install.sh if not already installed.")
    print("Do not keep a terminal window open as the availability strategy.")
    try:
        with socket.create_connection(("127.0.0.1", 8000), timeout=0.4):
            api = "up"
    except OSError:
        api = "down"
    print(f"backend: {api}")
    ts = probe()
    print(f"tailscale: {json.dumps({k: ts.get(k) for k in ('status', 'magic_dns', 'https_ready', 'funnel_enabled', 'private_url')}, indent=2)}")
    if os.environ.get("EV_TAILSCALE_SERVE_APPLY") == "1":
        print("apply serve:", maybe_apply_serve())
    else:
        print("Serve not applied. Recommended admin command:")
        print(" ", ts.get("recommended_serve"))
    snap = snapshot()
    print(f"home_station: {snap.get('home_station')}")
    print(f"power: {snap.get('power')}")
    print("macOS sleep: if the Mac sleeps, remote phones cannot reach Evie.")
    print("Use Energy Saver 'Prevent automatic sleeping when the display is off' for this Mac,")
    print("or caffeinate while testing. Do not disable all energy saving blindly.")
    print("ACL: phones should reach Serve HTTPS only — not Postgres/Redis/helper sockets.")
    print("Funnel must remain OFF.")
    if ts.get("funnel_enabled"):
        return 2
    return 0 if api == "up" else 1


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "backend"))
    raise SystemExit(main())
