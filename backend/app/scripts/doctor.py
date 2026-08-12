"""``make doctor`` — one screen explaining why EV feels slow.

Reads the running API's /v1/ops/metrics (latency, arbiter, restore-drill
age, warnings) and the host's system state (RAM, disk, swap, stack RSS,
service states). Exits 0: the point is a readable diagnosis, not a gate.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx


def _api_base() -> str:
    return os.environ.get("EV_API_URL", "http://127.0.0.1:8000").rstrip("/")


def _master_key() -> str:
    value = os.environ.get("EV_MASTER_KEY")
    if value:
        return value
    env_file = Path(__file__).resolve().parents[2] / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("EV_MASTER_KEY="):
                return line.split("=", 1)[1].strip()
    return ""


def _fetch_metrics() -> dict | None:
    try:
        response = httpx.get(
            f"{_api_base()}/v1/ops/metrics",
            headers={"Authorization": f"Bearer {_master_key()}"},
            timeout=8.0,
        )
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def _service_states() -> dict[str, str]:
    labels = ("api", "worker", "scheduler", "runtime", "ears", "collector")
    try:
        output = subprocess.run(
            ["launchctl", "list"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {label: "unknown" for label in labels}
    running = {line.split("\t")[-1] for line in output.splitlines() if "\t" in line}
    return {label: ("running" if f"ev.{label}" in running else "stopped") for label in labels}


def _brew_services() -> dict[str, str]:
    try:
        output = subprocess.run(
            ["brew", "services", "list"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    services: dict[str, str] = {}
    for line in output.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            services[parts[0]] = parts[1]
    return services


def _free_ram_mb() -> float | None:
    try:
        output = subprocess.run(
            ["vm_stat"],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    import re

    page_size = 16384
    match = re.search(r"page size of (\d+) bytes", output)
    if match:
        page_size = int(match.group(1))
    values: dict[str, int] = {}
    for line in output.splitlines():
        key, _, value = line.partition(":")
        value = value.strip().rstrip(".")
        try:
            values[key.strip()] = int(value)
        except ValueError:
            continue
    return round(
        (values.get("Pages free", 0) + values.get("Pages inactive", 0))
        * page_size
        / (1024 * 1024),
        1,
    )


def _swap_used_mb() -> float | None:
    try:
        output = subprocess.run(
            ["sysctl", "vm.swapusage"],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for part in output.replace("vm.swapusage:", "").split(","):
        key, _, value = part.strip().partition("=")
        if key.strip() == "used":
            try:
                return round(float(value.strip().split()[0]), 1)
            except (ValueError, IndexError):
                return None
    return None


def _disk_gb() -> float:
    return round(shutil.disk_usage(Path.home()).free / (1024**3), 2)


def main() -> int:
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    print(f"EV doctor — {now}")
    print("=" * 64)

    services = _service_states()
    brew = _brew_services()
    metrics = _fetch_metrics()

    print("\nSystem")
    ram = _free_ram_mb()
    ram_flag = "ok" if ram is None or ram >= 2048 else "WARN"
    print(f"  [{ram_flag}] reclaimable RAM: {ram:.0f} MB" if ram is not None else "  [?] reclaimable RAM: unknown")
    disk = _disk_gb()
    print(f"  [{'ok' if disk >= 5 else 'WARN'}] free disk: {disk:.1f} GB")
    swap = _swap_used_mb()
    if swap is not None:
        print(f"  [{'ok' if swap <= 512 else 'WARN'}] swap used: {swap:.0f} MB")
    if metrics:
        system = metrics.get("system") or {}
        rss = system.get("stack_rss_mb")
        if rss is not None:
            print(f"  [ok] EV stack RSS: {rss:.0f} MB")

    print("\nServices")
    for label in ("api", "worker", "scheduler", "runtime", "ears", "collector"):
        print(f"  [{services.get(label, 'unknown')}] ev.{label}")
    for name in ("postgresql@17", "postgresql@15", "redis"):
        state = brew.get(name)
        if state:
            print(f"  [{state}] brew {name}")

    print("\nAPI")
    if metrics is None:
        print("  [FAIL] API unreachable at " + _api_base())
        print("         That alone explains a dead EV. Check ev.api + postgres + redis.")
    else:
        latency = metrics.get("latency") or {}
        print(f"  [ok] p95 model latency: {latency.get('p95_ms')} ms")
        drill = metrics.get("restore_drill") or {}
        print(
            "  ["
            + ("WARN" if drill.get("stale") else "ok")
            + f"] restore drill age: {drill.get('age_days')} days"
        )
        arbiter = metrics.get("arbiter") or {}
        print(f"  [ok] ML resident: {arbiter.get('resident_total_mb')} MB / {arbiter.get('ceiling_mb')} MB ceiling")
        warnings = metrics.get("warnings") or []
        print(f"\nWarnings ({len(warnings)})")
        for warning in warnings:
            print(f"  ! {warning}")
        if not warnings:
            print("  none — if EV still feels slow, check network + model providers.")

    print("\nNext: run `make prune` for disk, `make native-status` for services.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
