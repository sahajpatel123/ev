"""Developer start: verify backend, PWA, Tailscale Serve, print private URL."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _tcp(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except OSError:
        return False


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=3) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    os.chdir(ROOT / "backend")
    print("== Evie cross-platform dev ==")
    if not _tcp("127.0.0.1", 8000):
        print("Backend 127.0.0.1:8000 is down.")
        print("Start Home Station API with: make dev   (already bound to localhost)")
        print("Do not bind 0.0.0.0 or enable Tailscale Funnel.")
        return 1
    health = _get("http://127.0.0.1:8000/v1/health")
    gw = _get("http://127.0.0.1:8000/v1/device-gateway/health")
    pwa_ok = False
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/evie/", timeout=3) as resp:
            pwa_ok = resp.status == 200
    except OSError:
        pwa_ok = False
    print(f"backend PID: {health.get('runtime', {}).get('pid')}")
    print(f"backend version: {health.get('version')}")
    print(f"PWA /evie/: {'ok' if pwa_ok else 'MISSING'}")
    print(f"protocol_version: {gw.get('protocol_version')}")
    print(f"pwa_build: {gw.get('pwa_build')}")
    print(f"production_memory_enabled: {gw.get('production_memory_enabled')}")
    print(f"home_station: {gw.get('home_station')}")
    print(f"publicly_exposed/funnel: {gw.get('funnel_enabled')}")
    ts = gw.get("tailscale") or {}
    print(f"tailscale: {ts.get('status')} magic_dns={ts.get('magic_dns')}")
    url = ts.get("private_url") or "https://<magicdns>/evie/"
    print(f"private URL: {url}")
    if not ts.get("https_ready"):
        print("Serve HTTPS is not confirmed. Recommended (admin, not auto-applied):")
        print("  tailscale serve --bg --https=443 http://127.0.0.1:8000")
        print("Set EV_TAILSCALE_SERVE_APPLY=1 only if you want this script to run that command.")
    if gw.get("funnel_enabled"):
        print("FAIL: Funnel appears enabled. Turn it off. Cross-platform v1 is private.")
        return 2
    if gw.get("production_memory_enabled"):
        print("FAIL: production_memory_enabled must be false in this phase.")
        return 3
    master = os.environ.get("EV_MASTER_KEY")
    if master:
        for role, name in (
            ("primary_companion", "Primary iPhone"),
            ("secondary_companion", "Secondary iPhone"),
        ):
            req = urllib.request.Request(
                "http://127.0.0.1:8000/v1/device-gateway/pairing-tokens",
                data=json.dumps({"role": role, "display_name": name}).encode(),
                headers={
                    "Authorization": f"Bearer {master}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=3) as resp:
                    minted = json.loads(resp.read().decode())
                print(f"pairing {role}: {minted.get('pairing_token')}")
            except OSError as exc:
                print(f"pairing {role}: could not mint ({exc})")
    print("Owner phones: Tailscale app → open private URL → Add to Home Screen → paste pairing token.")
    tests = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_device_gateway.py"],
        cwd=ROOT / "backend",
    )
    print("physical iPhone tests: NOT RUN (owner Tier B)")
    print("cellular tests: NOT RUN (owner Tier B)")
    return tests.returncode


if __name__ == "__main__":
    raise SystemExit(main())
