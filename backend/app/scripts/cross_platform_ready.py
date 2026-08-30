"""One-command Home Station prep for Evie Cross-Platform Core v1."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import urllib.error
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
    with urllib.request.urlopen(req, timeout=4) as resp:
        return json.loads(resp.read().decode())


def _print_matrix(rows: list[tuple[str, str]]) -> None:
    print("\n== Automated acceptance ==")
    for name, status in rows:
        print(f"{name:<36} {status}")


def main() -> int:
    os.chdir(ROOT / "backend")
    print("EVIE CROSS-PLATFORM READY")
    print("WHAT ACTUALLY RUNS NOW")
    api_up = _tcp("127.0.0.1", 8000)
    if not api_up:
        print("Backend 127.0.0.1:8000 is down. Starting is required for pairing URLs.")
        print("Home Station: launchd/ev.api.plist KeepAlive binds 127.0.0.1:8000")
        print("Start with: make evie-home-station   or   make dev")
    health = {}
    gw = {}
    pwa_ok = False
    if api_up:
        try:
            health = _get("http://127.0.0.1:8000/v1/health")
            gw = _get("http://127.0.0.1:8000/v1/device-gateway/health")
            with urllib.request.urlopen("http://127.0.0.1:8000/evie/", timeout=3) as resp:
                pwa_ok = resp.status == 200
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            print("health fetch failed:", exc)
    from app.device_gateway.sandbox_tools import provider_effective_snapshot
    from app.device_gateway.tailscale import maybe_apply_serve, probe

    ts = probe()
    if ts.get("logged_in") and not ts.get("serve_enabled") and not ts.get("funnel_enabled"):
        os.environ["EV_TAILSCALE_SERVE_APPLY"] = "1"
        applied = maybe_apply_serve()
        print("serve apply:", applied)
        ts = probe()
    tools = provider_effective_snapshot()
    print(f"Backend: {'up' if api_up else 'down'}")
    print(f"PID: {health.get('runtime', {}).get('pid') or gw.get('backend_pid')}")
    print(f"build: {gw.get('pwa_build')}")
    print(f"PWA: {'ok' if pwa_ok else 'MISSING'}  build: {gw.get('pwa_build')}  protocol: {gw.get('protocol_version')}")
    print(f"Tailscale: version={ts.get('tailscale_version')} logged_in={ts.get('logged_in')}")
    print(f"Serve: {ts.get('serve_enabled')}  Funnel: {ts.get('funnel_enabled')}  MagicDNS: {ts.get('magic_dns')}")
    print(f"private URL: {ts.get('private_url')}")
    print(f"Home Station: mode={gw.get('home_station_mode')} power={gw.get('power_source')} sleep={gw.get('sleep_prevention_active')}")
    print(f"Sandbox: production memory enabled={gw.get('production_memory_enabled')}")
    print(f"Realtime sandbox tools hash: {tools.get('sandbox_tool_schema_hash')} ready={tools.get('live_cross_platform_tools_ready')}")

    tests = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_device_gateway.py"],
        cwd=ROOT / "backend",
    )
    print("automated gateway tests:", "PASS" if tests.returncode == 0 else "FAIL")

    matrix = [
        ("Tailnet detected", "PASS" if ts.get("installed") and ts.get("logged_in") else "FAIL"),
        ("Serve configured", "PASS" if ts.get("serve_enabled") else "FAIL"),
        ("Private HTTPS", "PASS" if ts.get("https_ready") else "FAIL"),
        ("MagicDNS", "PASS" if ts.get("magic_dns") else "FAIL"),
        ("Funnel off", "PASS" if not ts.get("funnel_enabled") else "FAIL"),
        ("Backend localhost only", "PASS" if gw.get("backend_localhost_only", True) else "FAIL"),
        ("Same-origin PWA", "PASS" if pwa_ok else "FAIL"),
        ("Production memory firewall", "PASS" if gw.get("production_memory_enabled") is False else "FAIL"),
        ("Sandbox tools local catalog", "PASS" if tools.get("local_catalog_ready") else "FAIL"),
        ("Provider schema verified", "PASS" if tools.get("live_provider_tools_verified") else "FAIL"),
        ("PWA versioning", "PASS" if (gw.get("pwa_build") or "").startswith("2026.08") else "FAIL"),
        ("launchd plist present", "PASS" if gw.get("launchd_plist_present") else "FAIL"),
        ("Automated tests", "PASS" if tests.returncode == 0 else "FAIL"),
    ]
    _print_matrix(matrix)

    print("\n== Owner pairing ==")
    master = os.environ.get("EV_MASTER_KEY")
    if not master:
        env_path = ROOT / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("EV_MASTER_KEY="):
                    master = line.split("=", 1)[1].strip().strip('"')
                    break
    pairing = {}
    if api_up and master:
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
                with urllib.request.urlopen(req, timeout=4) as resp:
                    minted = json.loads(resp.read().decode())
                pairing[role] = minted.get("pairing_token")
                print(f"{role}: {minted.get('pairing_token')}")
            except OSError as exc:
                print(f"{role}: could not mint ({exc})")
    else:
        print("Pairing not minted (API down or EV_MASTER_KEY missing).")
        print("After API is up:  uv run python -m app.scripts.evie_devices pair-primary")

    if ts.get("one_action_required") and not ts.get("logged_in"):
        print("\nONE ACTION REQUIRED:")
        print(ts.get("one_action_required"))
        print("Then rerun: make evie-cross-platform-ready")

    print("\nEVIE CROSS-PLATFORM READY FOR PHYSICAL TEST" if ts.get("https_ready") else "\nEVIE CROSS-PLATFORM PARTIAL — Tailscale HTTPS not live")
    print(f"Private URL: {ts.get('private_url') or 'unavailable until Tailscale Serve'}")
    print(f"PRIMARY PAIRING: {pairing.get('primary_companion') or 'run evie_devices pair-primary'}")
    print(f"SECONDARY PAIRING: {pairing.get('secondary_companion') or 'run evie_devices pair-secondary'}")
    print(
        "On each phone:\n"
        "1. Open Tailscale.\n"
        "2. Open private Evie URL.\n"
        "3. Add to Home Screen.\n"
        "4. Pair.\n"
        "5. Tap Run Device Self-Test."
    )
    print("PHYSICAL IPHONE VERIFIED: PENDING")
    print("CELLULAR VERIFIED: PENDING")
    return 0 if tests.returncode == 0 else tests.returncode


if __name__ == "__main__":
    raise SystemExit(main())
