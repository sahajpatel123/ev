"""Tailscale discovery and Serve. Never enable Funnel. Never mutate ACLs."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

_CANDIDATES = (
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    "/usr/local/bin/tailscale",
    "/opt/homebrew/bin/tailscale",
    str(Path.home() / "Applications/Tailscale.app/Contents/MacOS/Tailscale"),
)


def find_binary() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    for path in _CANDIDATES:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def probe() -> dict[str, Any]:
    binary = find_binary()
    if not binary:
        return {
            "status": "missing",
            "installed": False,
            "binary": None,
            "https_ready": False,
            "serve_enabled": False,
            "funnel_enabled": False,
            "private_url": None,
            "one_action_required": (
                "Install Tailscale (App Store or `brew install --cask tailscale-app`), "
                "sign in to the tailnet, then rerun `make evie-cross-platform-ready`."
            ),
            "acl_note": (
                "Prefer tags so phones can reach Serve HTTPS only — "
                "not Postgres, Redis, RQ, or helper sockets. Do not enable Funnel."
            ),
        }
    version = _text_cmd([binary, "version"]).splitlines()[0] if _text_cmd([binary, "version"]) else ""
    status = _json_cmd([binary, "status", "--json"])
    serve_json = _json_cmd([binary, "serve", "status", "--json"])
    serve_text = _text_cmd([binary, "serve", "status"])
    funnel_text = _text_cmd([binary, "funnel", "status"])
    help_text = _text_cmd([binary, "serve", "--help"])
    dns_name = None
    backend_state = None
    logged_in = False
    if isinstance(status, dict):
        self_node = status.get("Self") or {}
        dns_name = self_node.get("DNSName")
        if isinstance(dns_name, str):
            dns_name = dns_name.rstrip(".")
        backend_state = status.get("BackendState")
        logged_in = backend_state == "Running" and bool(self_node)
    funnel = _funnel_on(serve_json, serve_text, funnel_text)
    serve_enabled = _serve_on(serve_json, serve_text)
    https_ready = serve_enabled and not funnel and bool(dns_name)
    private_url = f"https://{dns_name}/evie/" if dns_name else None
    ts_status = "ok" if backend_state == "Running" else str(backend_state or "unknown").lower()
    if backend_state == "Stopped":
        ts_status = "down"
    if backend_state in {"NeedsLogin", "NoState"}:
        ts_status = "needs_login"
    apply = os.environ.get("EV_TAILSCALE_SERVE_APPLY") == "1" or bool(
        getattr(__import__("app.config", fromlist=["settings"]).settings, "tailscale_serve_apply", False)
    )
    serve_cmd = _serve_command(help_text)
    return {
        "status": ts_status,
        "installed": True,
        "binary": binary,
        "tailscale_version": version,
        "backend_state": backend_state,
        "logged_in": logged_in,
        "magic_dns": dns_name,
        "https_ready": https_ready,
        "serve_enabled": serve_enabled,
        "funnel_enabled": funnel,
        "serve_status": (serve_text or json.dumps(serve_json or {}))[:2000],
        "private_url": private_url,
        "serve_apply_requested": apply,
        "recommended_serve": serve_cmd,
        "serve_help_excerpt": help_text[:800],
        "acl_note": (
            "Prefer tags so phones can reach this Serve HTTPS port only — "
            "not Postgres, Redis, RQ, or helper sockets. Do not enable Funnel."
        ),
        "one_action_required": None
        if logged_in
        else f"Open Tailscale and sign in, then rerun setup. CLI: {binary} up",
    }


def maybe_apply_serve() -> dict[str, Any]:
    """Apply Serve when authorized. Never enable Funnel."""

    apply = os.environ.get("EV_TAILSCALE_SERVE_APPLY") == "1"
    if not apply:
        return {"applied": False, "reason": "set EV_TAILSCALE_SERVE_APPLY=1 to run serve"}
    probed = probe()
    binary = probed.get("binary")
    if not binary:
        return {"applied": False, "reason": "tailscale binary missing"}
    if probed.get("funnel_enabled"):
        return {"applied": False, "reason": "Funnel is on; turn it off before Serve"}
    cmd = str(probed.get("recommended_serve") or "serve --bg --https=443 http://127.0.0.1:8000").split()
    if not cmd or cmd[0] != "serve":
        cmd = ["serve", "--bg", "--https=443", "http://127.0.0.1:8000"]
    parts = [str(binary), *cmd]
    try:
        out = subprocess.check_output(parts, timeout=12, stderr=subprocess.STDOUT, text=True)
        return {"applied": True, "funnel": False, "output": out[:500]}
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return {"applied": False, "reason": str(exc)[:400]}


def _serve_command(help_text: str) -> str:
    text = help_text.lower()
    if "--bg" in text and "--https" in text:
        return "serve --bg --https=443 http://127.0.0.1:8000"
    if "https" in text:
        return "serve --bg --https=443 http://127.0.0.1:8000"
    return "serve --bg --https=443 http://127.0.0.1:8000"


def _funnel_on(serve_json: dict | None, serve_text: str, funnel_text: str = "") -> bool:
    blob = json.dumps(serve_json or {}).lower() + "\n" + (serve_text or "").lower() + "\n" + (funnel_text or "").lower()
    if "funnel" not in blob:
        return False
    return any(token in blob for token in ("funnel=true", '"funnel": true', "funnel on", "funnel enabled"))


def _serve_on(serve_json: dict | None, serve_text: str) -> bool:
    if isinstance(serve_json, dict) and serve_json:
        return True
    text = (serve_text or "").lower()
    return "http://127.0.0.1:8000" in text or "https://" in text and "proxy" in text


def _json_cmd(cmd: list[str]) -> dict[str, Any] | None:
    try:
        out = subprocess.check_output(cmd, timeout=4, stderr=subprocess.STDOUT, text=True)
        data = json.loads(out)
        return data if isinstance(data, dict) else None
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def _text_cmd(cmd: list[str]) -> str:
    try:
        return subprocess.check_output(cmd, timeout=4, stderr=subprocess.STDOUT, text=True)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ""
