"""Environment + permission probes shared by the /v1/fabric/permissions
route and `ev setup` / `ev doctor`. Every probe is prompt-free, fast, and
never raises: uncheckable things report "unknown" with a fix, never a guess.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

TCC_FIX = "Run `ev setup` and grant it in System Settings when prompted."


def entry(permission: str, kind: str, status: str, detail: str = "",
          fix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {"permission": permission, "kind": kind,
                           "status": status, "detail": detail}
    if status != "granted" and fix:
        out["fix"] = fix
    return out


def run_probe(name: str, kind: str, check: Callable[[], tuple[str, str]],
              fix: str) -> dict[str, Any]:
    try:
        status, detail = check()
    except Exception as exc:
        status, detail = "unknown", f"probe failed: {type(exc).__name__}"
    return entry(name, kind, status, detail, fix)


def probe_tmux() -> dict[str, Any]:
    from . import terminal

    def check() -> tuple[str, str]:
        if not terminal.has_tmux():
            return "denied", "tmux not installed"
        try:
            p = subprocess.run(["tmux", "-V"], capture_output=True, text=True, timeout=5)
            return "granted", (p.stdout or "").strip()[:80]
        except Exception as exc:
            return "unknown", str(exc)[:120]

    return run_probe("tmux", "tool", check, "Install tmux: `brew install tmux`.")


def probe_ghostty() -> dict[str, Any]:
    from . import terminal

    def check() -> tuple[str, str]:
        probed = terminal.probe_ghostty()["evidence"]
        if not probed.get("bundle_present"):
            return "denied", "Ghostty.app not installed"
        first = str(probed.get("version") or "installed").strip().splitlines()
        return "granted", (first[0] if first else "installed")[:80]

    return run_probe("ghostty", "tool", check, "Install Ghostty from ghostty.org.")


def probe_tailscale() -> dict[str, Any]:
    def check() -> tuple[str, str]:
        cli = shutil.which("tailscale")
        app_cli = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
        binary = cli or (app_cli if Path(app_cli).exists() else None)
        if binary is None:
            return "denied", "Tailscale not installed"
        try:
            p = subprocess.run([binary, "status"], capture_output=True, text=True, timeout=10)
            out = ((p.stdout or "") + (p.stderr or "")).strip()[:200]
            if "stopped" in out.lower():
                return "denied", "Tailscale is stopped"
            funnel = "funnel" in out.lower()
            return "granted", (out or "running") + ("; FUNNEL MENTIONED" if funnel else "")
        except Exception as exc:
            return "unknown", str(exc)[:120]

    return run_probe("tailscale", "tool", check,
                     "Install Tailscale, sign in, then `ev setup phone`.")


def probe_which(tool: str, fix: str) -> dict[str, Any]:
    def check() -> tuple[str, str]:
        found = shutil.which(tool)
        return ("granted", found or "") if found else ("denied", f"{tool} not on PATH")

    return run_probe(tool, "tool", check, fix)


def probe_evie_app() -> dict[str, Any]:
    def check() -> tuple[str, str]:
        bundle = Path("/Applications/Evie.app")
        if not bundle.exists():
            return "denied", "Evie.app not installed"
        try:
            p = subprocess.run(["codesign", "-dvv", str(bundle)], capture_output=True,
                               text=True, timeout=10)
            ident = ""
            for line in ((p.stderr or "") + (p.stdout or "")).splitlines():
                if line.startswith("Authority="):
                    ident = line
                    break
            return "granted", ident[:120] or "installed"
        except Exception as exc:
            return "unknown", str(exc)[:120]

    return run_probe("evie.app", "app", check,
                     "Reinstall Evie.app, then re-grant TCC items.")


def probe_launchd(label: str) -> dict[str, Any]:
    def check() -> tuple[str, str]:
        try:
            p = subprocess.run(["launchctl", "list", label], capture_output=True,
                               text=True, timeout=10)
        except Exception as exc:
            return "unknown", str(exc)[:120]
        if p.returncode != 0:
            return "denied", f"{label} not loaded"
        pid = ""
        for line in (p.stdout or "").splitlines():
            if '"PID"' in line or line.strip().startswith('"PID"'):
                pid = line.strip()[:40]
                break
        return "granted", f"{label} loaded {pid}".strip()

    return run_probe(f"launchd:{label}", "service", check,
                     f"Load it: `launchctl load ~/Library/LaunchAgents/{label}.plist`.")


def probe_bridge() -> dict[str, Any]:
    from .bridge_client import BridgeClient

    def check() -> tuple[str, str]:
        res = BridgeClient(timeout=5).call("ping")
        if res.get("ok"):
            return "granted", "Evie.app bridge answering"
        return "denied", str(res.get("error") or "bridge down")

    return run_probe("bridge", "service", check, "Launch Evie.app.")


def tcc_entries() -> list[dict[str, Any]]:
    """App-identity TCC truth via the bridge; honest unknowns when down."""
    from .bridge_client import BridgeClient

    names = ["accessibility", "full_disk_access", "screen_recording",
             "microphone", "input_monitoring", "contacts", "calendars", "reminders"]
    # Note: no aggregate "automation" row exists on purpose — Automation is
    # per-target-app and only provable by attempting (see ev setup).
    res = BridgeClient(timeout=10).call("tcc_status")
    by_name: dict[str, dict[str, Any]] = {}
    if res.get("ok"):
        for row in res.get("evidence", {}).get("permissions", []) or []:
            if isinstance(row, dict) and row.get("permission"):
                by_name[str(row["permission"])] = row
    out = []
    for name in names:
        row = by_name.get(name)
        if row is not None and row.get("status") in {"granted", "denied", "unknown"}:
            out.append(entry(name, "tcc", str(row["status"]),
                             str(row.get("detail", "")), TCC_FIX))
        else:
            out.append(entry(name, "tcc", "unknown", "Evie.app bridge not answering",
                             "Launch Evie.app, then re-check."))
    return out


def environment_entries() -> list[dict[str, Any]]:
    return [
        probe_tmux(),
        probe_ghostty(),
        probe_tailscale(),
        probe_which("docker", "Install Docker Desktop (optional; scratch Postgres)."),
        probe_which("osascript", "Install Xcode Command Line Tools."),
        probe_evie_app(),
        probe_bridge(),
    ]


def permission_entries() -> list[dict[str, Any]]:
    """Full /v1/fabric/permissions payload (tools + app + TCC)."""
    return environment_entries() + tcc_entries()
