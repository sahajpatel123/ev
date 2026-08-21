"""Home Station sleep prevention. Tied to API process; no orphan caffeinate."""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
from typing import Any

_PROC: subprocess.Popen | None = None


def power_source() -> str:
    pmset = shutil.which("pmset")
    if not pmset:
        return "unknown"
    try:
        out = subprocess.check_output([pmset, "-g", "batt"], timeout=2, text=True)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return "unknown"
    if "AC Power" in out:
        return "ac"
    if "Battery Power" in out:
        return "battery"
    return "unknown"


def should_hold_assertion(*, mode_on: bool, keep_ac: bool, keep_battery: bool) -> bool:
    if not mode_on:
        return False
    source = power_source()
    if source == "ac":
        return bool(keep_ac)
    if source == "battery":
        return bool(keep_battery)
    return bool(keep_ac)


def start_if_needed(*, mode_on: bool, keep_ac: bool, keep_battery: bool) -> dict[str, Any]:
    global _PROC
    want = should_hold_assertion(mode_on=mode_on, keep_ac=keep_ac, keep_battery=keep_battery)
    if not want:
        stop()
        return {
            "home_station_mode": mode_on,
            "sleep_prevention_active": False,
            "power_source": power_source(),
        }
    binary = shutil.which("caffeinate")
    if binary is None:
        return {
            "home_station_mode": mode_on,
            "sleep_prevention_active": False,
            "power_source": power_source(),
            "detail": "caffeinate unavailable",
        }
    if _PROC is not None and _PROC.poll() is None:
        return {
            "home_station_mode": mode_on,
            "sleep_prevention_active": True,
            "power_source": power_source(),
        }
    _PROC = subprocess.Popen(
        [binary, "-s", "-w", str(os.getpid())],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {
        "home_station_mode": mode_on,
        "sleep_prevention_active": True,
        "power_source": power_source(),
        "caffeinate_pid": _PROC.pid,
    }


def stop() -> None:
    global _PROC
    if _PROC is None:
        return
    with contextlib.suppress(OSError):
        _PROC.terminate()
    _PROC = None


def snapshot() -> dict[str, Any]:
    alive = _PROC is not None and _PROC.poll() is None
    return {
        "sleep_prevention_active": alive,
        "power_source": power_source(),
        "caffeinate_pid": None if not alive or _PROC is None else _PROC.pid,
    }
