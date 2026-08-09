"""End-to-end CLI validation: boot a real server and drive the `ev` client.

This is the fast whole-stack check for the web/CLI surface: capture, memory
search, chat, HUD card, tactical quick card, export/import round-trip,
onboarding, and offline queue/sync, all through the actual client executable
against a live uvicorn process.

Usage:
    python -m app.scripts.e2e_cli
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
MASTER_KEY = "e2e-key"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_healthy(base_url: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/v1/health", timeout=2) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(0.25)
    raise RuntimeError(f"server did not become healthy at {base_url}")


def run_cli(base_url: str, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["EV_API_URL"] = base_url
    env["EV_API_KEY"] = MASTER_KEY
    result = subprocess.run(
        [sys.executable, "-m", "clients.cli", *args],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"`ev {' '.join(args)}` failed ({result.returncode}):\n{result.stdout}\n{result.stderr}"
        )
    return result


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="ev-e2e-")
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env.update(
        {
            "EV_DATABASE_URL": f"sqlite+aiosqlite:///{tmp}/e2e.db",
            "EV_MASTER_KEY": MASTER_KEY,
            "EV_PROCESSING_MODE": "sync",
            "EV_CHAT_PROVIDER": "mock",
            "EV_EMBEDDING_PROVIDER": "hash",
            "EV_EMBEDDING_DIM": "64",
            "EV_STORAGE_ROOT": f"{tmp}/storage",
            "EV_ACCESS_LOG_ENABLED": "true",
            "EV_QUIET_HOURS_START": "23:59",
            "EV_QUIET_HOURS_END": "00:00",
        }
    )

    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=BACKEND,
        env=env,
    )
    checks: list[tuple[list[str], str, str]] = [
        (["capture", "Remember: e2e fixed-term contracts"], "captured ", "capture failed"),
        (["timeline"], "e2e fixed-term", "capture missing from timeline"),
        (["memories", "--search", "fixed-term"], "total:", "memory search failed"),
        (["ask", "What do I prefer?"], "", "ask failed"),
        (["card"], "[ev.hud.card.v1]", "HUD card failed"),
        (["quickcard", "E2E review"], "[ev.hud.quickcard.v1]", "quick card failed"),
        (["doctor"], "status: ok", "doctor failed"),
        (["queue"], "queue is empty", "queue not empty"),
        (["sync"], "synced 0", "sync failed"),
    ]
    try:
        wait_healthy(base_url)
        attachment = Path(tmp) / "note.txt"
        attachment.write_text("E2E attachment capture integrity check.", encoding="utf-8")
        checks.insert(1, (["attach", str(attachment)], "attached ", "attach failed"))
        for args, expected, label in checks:
            result = run_cli(base_url, args)
            if expected and expected not in result.stdout:
                raise RuntimeError(f"{label}: expected {expected!r} in:\n{result.stdout}")
            print(f"ok: ev {' '.join(args)}")

        # Export -> import round-trip (same DB: events dedupe by content hash).
        bundle = Path(tmp) / "bundle.json"
        run_cli(base_url, ["export", "--output", str(bundle)])
        result = run_cli(base_url, ["import", str(bundle), "--mode", "merge"])
        if "imported 0 events" not in result.stdout or "skipped" not in result.stdout:
            raise RuntimeError(f"import round-trip failed:\n{result.stdout}")
        print("ok: ev export -> import round-trip")

        # Onboarding (non-interactive) creates onboarding events + audits.
        result = run_cli(
            base_url,
            ["onboarding", "First: e2e goal", "Second: e2e person"],
        )
        if "EV remembers 2 things." not in result.stdout or "audit:" not in result.stdout:
            raise RuntimeError(f"onboarding failed:\n{result.stdout}")
        print("ok: ev onboarding")

        print(f"\nE2E CLI validation passed ({base_url})")
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    raise SystemExit(main())
