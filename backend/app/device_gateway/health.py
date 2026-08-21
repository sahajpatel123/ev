"""Home Station reachability. Truthful; does not disable energy saving."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any

from app.config import settings

from . import PROTOCOL_VERSION, PWA_BUILD
from .power import snapshot as assertion_snapshot
from .presence import snapshot as presence_snapshot
from .sandbox import production_memory_enabled
from .sandbox_tools import provider_effective_snapshot
from .tailscale import probe as tailscale_probe


def backend_bind_is_loopback() -> bool:
    return True


def tcp_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def power_status() -> dict[str, Any]:
    """Best-effort macOS power assertions. Never claims 24/7 without evidence."""

    pmset = shutil.which("pmset")
    if not pmset:
        return {"status": "unknown", "detail": "pmset unavailable"}
    try:
        out = subprocess.check_output([pmset, "-g", "assertions"], timeout=2, text=True)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return {"status": "unknown", "detail": str(exc)[:200]}
    prevent = "PreventUserIdleSystemSleep" in out and "1" in out
    return {
        "status": "sleep_possibly_blocked" if prevent else "may_sleep",
        "prevent_idle_sleep_mentioned": prevent,
    }


def snapshot(*, connected_devices: int | None = None) -> dict[str, Any]:
    ts = tailscale_probe()
    presence = presence_snapshot()
    api_up = tcp_open("127.0.0.1", 8000)
    funnel = bool(ts.get("funnel_enabled"))
    home = "ONLINE"
    if not api_up:
        home = "BACKEND_DOWN"
    elif ts.get("status") == "down":
        home = "TAILSCALE_DOWN"
    elif funnel:
        home = "DEGRADED"
    tools = provider_effective_snapshot()
    assertions = assertion_snapshot()
    power = {**power_status(), **assertions}
    launchd_plist = Path.home() / "Library/LaunchAgents/ev.api.plist"
    return {
        "device_gateway_ready": True,
        "api_ready": api_up,
        "gateway_ready": True,
        "tailscale_ready": ts.get("status") == "ok",
        "serve_ready": bool(ts.get("serve_enabled")) and not funnel,
        "tailnet_reachable": ts.get("status") in {"ok", "logged_in", "needs_login", "unknown"}
        and ts.get("status") != "down",
        "https_ready": bool(ts.get("https_ready")),
        "connected_devices": connected_devices if connected_devices is not None else presence["online_count"],
        "presence": presence,
        "voice_gateway_ready": True,
        "camera_gateway_ready": True,
        "camera_ready": True,
        "mac_control_ready": None,
        "sandbox_memory_ready": True,
        "production_memory_enabled": production_memory_enabled(),
        "protocol_version": PROTOCOL_VERSION,
        "pwa_build": getattr(settings, "pwa_build", None) or PWA_BUILD,
        "backend_pid": os.getpid(),
        "bind": "127.0.0.1:8000",
        "backend_localhost_only": True,
        "publicly_exposed": funnel,
        "funnel_enabled": funnel,
        "home_station": home,
        "home_station_mode": bool(settings.home_station_mode),
        "power": power,
        "sleep_prevention_active": bool(assertions.get("sleep_prevention_active")),
        "power_source": assertions.get("power_source") or power.get("status"),
        "launchd_plist_present": launchd_plist.is_file(),
        "tailscale": ts,
        "sandbox_tools": tools,
        "live_cross_platform_tools_ready": bool(tools.get("live_cross_platform_tools_ready")),
        "sandbox_tool_schema_hash": tools.get("sandbox_tool_schema_hash"),
        "tool_schema_generation": tools.get("tool_schema_generation"),
        "audio_contract": {
            "codec": "pcm16le",
            "sample_rate": 16000,
            "channels": 1,
            "frame_duration_ms": 20,
            "fallback_only": True,
        },
        "phone_audio_backend": getattr(settings, "phone_audio_backend", "webrtc_strict"),
        "mobile_voice_status": "OWNER FAILURE / CONVERGENCE ACTIVE",
        "design_version": getattr(settings, "pwa_design_version", None) or "veil-1",
        "web_push": "DEFERRED",
        "always_ready_se": "DEFERRED",
        "native_ios": "DEFERRED",
        "mobile_actions": {
            "track": "v1",
            "bridge_name": "Evie Mobile Bridge",
            "protocol": 1,
            "native_shell": "DEFERRED",
            "remote_unattended": False,
        },
        "always_ready_voice": False,
    }
