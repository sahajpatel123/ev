"""End-to-end CLI validation against a live EV backend.

This is the fast whole-stack check for the web/CLI surface: capture, memory
search, chat, HUD card, tactical quick card, export/import round-trip,
onboarding, offline queue/sync, and (against the compose/Postgres stack) the
queue worker, scheduler, and 24/7 runtime daemon.

Usage:
    # Default: boots a local SQLite + sync server.
    python -m app.scripts.e2e_cli

    # Against the compose stack (Postgres + Redis + MinIO + queue workers):
    EV_E2E_BASE_URL=http://127.0.0.1:8000 EV_E2E_MASTER_KEY=e2e-key \
        python -m app.scripts.e2e_cli

    # Local server on Postgres with queue processing (dev):
    EV_E2E_DATABASE_URL=postgresql+psycopg://... EV_E2E_PROCESSING_MODE=queue \
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
from typing import Any

import httpx

BACKEND = Path(__file__).resolve().parents[2]
MASTER_KEY = os.environ.get("EV_E2E_MASTER_KEY", "e2e-key")
QUEUE_TIMEOUT_SECONDS = float(os.environ.get("EV_E2E_QUEUE_TIMEOUT", "120"))
SCHEDULER_TIMEOUT_SECONDS = float(os.environ.get("EV_E2E_SCHEDULER_TIMEOUT", "180"))
DAEMON_TIMEOUT_SECONDS = float(os.environ.get("EV_E2E_DAEMON_TIMEOUT", "120"))
REAL_VOICE_REQUIRED = os.environ.get("EV_E2E_EXPECT_REAL_VOICE", "0") == "1"
TEST_DOUBLE_ALGORITHMS = {"profile-v1", "hash"}


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_healthy(base_url: str, timeout: float = 60.0) -> None:
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


def http_json(base_url: str, method: str, path: str, payload: dict | None = None) -> Any:
    with httpx.Client(
        base_url=base_url,
        headers={"Authorization": f"Bearer {MASTER_KEY}"},
        timeout=30,
    ) as client:
        response = client.request(method, path, json=payload)
        if response.status_code >= 400:
            raise RuntimeError(
                f"{method} {path} -> HTTP {response.status_code}: {response.text[:800]}"
            )
        return response.json()


def wait_for(predicate, description: str, timeout: float, interval: float = 2.0):
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except Exception as exc:  # noqa: BLE001 - polling boundary
            last_error = exc
        time.sleep(interval)
    suffix = f" (last error: {last_error})" if last_error else ""
    raise RuntimeError(f"timed out after {timeout:.0f}s waiting for {description}{suffix}")


def timeline_text(base_url: str) -> str:
    return run_cli(base_url, ["timeline"]).stdout


def memory_search_text(base_url: str) -> str:
    return run_cli(base_url, ["memories", "--search", "fixed-term"]).stdout


def daemon_tick_seen(base_url: str) -> bool:
    data = http_json(base_url, "GET", "/v1/runtime/sync")
    return any(
        event.get("kind") == "daemon" and "overall" in (event.get("payload") or {})
        for event in data.get("events", [])
    )


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="ev-e2e-")
    external_url = os.environ.get("EV_E2E_BASE_URL", "").strip().rstrip("/")
    local = not external_url
    port = free_port()
    base_url = external_url or f"http://127.0.0.1:{port}"
    server = None
    env = os.environ.copy()
    if local:
        database_url = os.environ.get(
            "EV_E2E_DATABASE_URL", f"sqlite+aiosqlite:///{tmp}/e2e.db"
        )
        processing_mode = os.environ.get("EV_E2E_PROCESSING_MODE", "sync")
        env.update(
            {
                "EV_DATABASE_URL": database_url,
                "EV_MASTER_KEY": MASTER_KEY,
                "EV_VAULT_KEY": os.environ.get("EV_E2E_VAULT_KEY", "e2e-vault-key-0123456789abcdef"),
                "EV_PROCESSING_MODE": processing_mode,
                "EV_CHAT_PROVIDER": os.environ.get("EV_E2E_CHAT_PROVIDER", "mock"),
                "EV_EMBEDDING_PROVIDER": os.environ.get("EV_E2E_EMBEDDING_PROVIDER", "hash"),
                "EV_EMBEDDING_DIM": os.environ.get("EV_E2E_EMBEDDING_DIM", "384"),
                "EV_STORAGE_ROOT": os.environ.get("EV_E2E_STORAGE_ROOT", f"{tmp}/storage"),
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

    queue_mode = os.environ.get("EV_E2E_EXPECT_QUEUE", "1" if external_url else "0") == "1"
    stack_workers = (
        os.environ.get("EV_E2E_EXPECT_STACK_WORKERS", "1" if external_url else "0") == "1"
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
        # Web workbench smoke: /app serves the SPA with a strict CSP.
        with urllib.request.urlopen(f"{base_url}/app", timeout=10) as web_resp:
            web_body = web_resp.read(4096)
            if web_resp.status != 200 or b"I\xe2\x80\x99m in your menu bar" not in web_body:
                raise RuntimeError("web /app did not serve the EVIE presence page")
            csp = web_resp.headers.get("Content-Security-Policy", "")
            if "default-src 'self'" not in csp:
                raise RuntimeError("web presence CSP missing default-src 'self'")
        with urllib.request.urlopen(f"{base_url}/app/ops", timeout=10) as ops_resp:
            ops_body = ops_resp.read(4096)
            if ops_resp.status != 200 or b"EV \xe2\x80\x94 Workbench" not in ops_body:
                raise RuntimeError("web /app/ops did not serve the operator console")
        print("ok: web presence /app + ops console + CSP")
        attachment = Path(tmp) / "note.txt"
        attachment.write_text("E2E attachment capture integrity check.", encoding="utf-8")
        checks.insert(1, (["attach", str(attachment)], "attached ", "attach failed"))
        samples = [Path(tmp) / f"e2e-voice-{index}.wav" for index in range(5)]
        for sample in samples:
            sample.write_bytes(b"e2e-voice-sample-" + b"x" * 256)
        voice_checks: list[tuple[list[str], str, str]] = []
        if REAL_VOICE_REQUIRED:
            voice_checks = [
                (
                    ["voice-enroll", *[str(s) for s in samples], "--liveness", "live"],
                    "enrolled ",
                    "voice enroll failed",
                ),
                (
                    ["voice-verify", *[str(s) for s in samples]],
                    "accepted: True",
                    "voice verify failed",
                ),
            ]
        checks.extend(
            [
                (
                    ["consent", "grant", "voice_enrollment"],
                    "consent granted: voice_enrollment",
                    "consent grant failed",
                ),
                *voice_checks,
                (["routines", "list"], "routines ", "routines list failed"),
                (["ops"], "focus:", "ops failed"),
                (["filter-report"], "", "filter report failed"),
            ]
        )

        for args, expected, label in checks[:2]:
            result = run_cli(base_url, args)
            if expected and expected not in result.stdout:
                raise RuntimeError(f"{label}: expected {expected!r} in:\n{result.stdout}")
            print(f"ok: ev {' '.join(args)}")

        if queue_mode:
            wait_for(
                lambda: "e2e fixed-term" in timeline_text(base_url),
                "queue worker to write capture to timeline",
                QUEUE_TIMEOUT_SECONDS,
                3.0,
            )
            wait_for(
                lambda: "[attachment/file]" in timeline_text(base_url),
                "attachment event to appear in timeline",
                QUEUE_TIMEOUT_SECONDS,
                3.0,
            )
            wait_for(
                lambda: "fixed-term" in memory_search_text(base_url),
                "queue worker to write searchable memory",
                QUEUE_TIMEOUT_SECONDS,
                3.0,
            )
            print("ok: queue worker processed captures and memories")

        for args, expected, label in checks[2:]:
            result = run_cli(base_url, args)
            if expected and expected not in result.stdout:
                raise RuntimeError(f"{label}: expected {expected!r} in:\n{result.stdout}")
            print(f"ok: ev {' '.join(args)}")

        if stack_workers:
            # 24/7 runtime daemon: its own process records a structured tick event.
            wait_for(
                lambda: daemon_tick_seen(base_url),
                "runtime daemon tick (kind=daemon runtime event)",
                DAEMON_TIMEOUT_SECONDS,
                5.0,
            )
            print("ok: runtime daemon tick observed")

            # Scheduler: create a due scheduled routine and wait for the scheduler
            # worker (not a manual/API run) to pick it up and execute it.
            routine = http_json(
                base_url,
                "POST",
                "/v1/routines",
                {
                    "name": f"e2e-scheduler-{int(time.time())}",
                    "kind": "scheduled",
                    "schedule": "* * * * *",
                    "timezone": "UTC",
                    "quiet_hours_skip": False,
                    "backfill_max": 1,
                    "cooldown_seconds": 0,
                    "trigger": {},
                    "action_type": "hud_card",
                    "action_title": "E2E scheduler tick",
                    "action_payload": {"title": "e2e scheduler tick", "kind": "note"},
                    "requires_approval": False,
                    "undoable": False,
                    "metadata": {"e2e": "postgres"},
                },
            )
            routine_id = str(routine["id"])
            print(f"ok: created scheduled routine {routine_id} for scheduler exercise")

            def scheduler_run() -> dict | None:
                runs = http_json(base_url, "GET", f"/v1/routines/{routine_id}/runs")
                return runs[0] if runs else None

            run = wait_for(
                scheduler_run,
                "scheduler worker to execute the scheduled routine",
                SCHEDULER_TIMEOUT_SECONDS,
                5.0,
            )
            if run.get("status") != "executed":
                raise RuntimeError(f"scheduler run did not execute: {run}")
            print(f"ok: scheduler executed routine run {run['id']} (status={run['status']})")

            # Notification delivery receipt through the production path:
            # dispatch -> backend receipt -> delivered status visible in the ledger.
            notification = http_json(
                base_url,
                "POST",
                "/v1/runtime/notify",
                {
                    "title": "E2E native stack",
                    "body": "notification delivery proof",
                    "priority": 0.5,
                    "tier": "useful",
                    "kind": "e2e",
                    "source": "e2e_cli",
                    "emergency": False,
                },
            )
            notification_id = str(notification["id"])
            delivered = [
                item
                for item in http_json(
                    base_url, "GET", "/v1/runtime/notifications?status=delivered"
                )
                if str(item["id"]) == notification_id
            ]
            if not delivered:
                raise RuntimeError(
                    f"notification {notification_id} has no delivered receipt; "
                    f"status={notification.get('status')} reason={notification.get('reason')}"
                )
            print(
                f"ok: notification delivered via {delivered[0].get('backend')} "
                f"(receipt {notification_id})"
            )

            # Voice round trip proof (when requested): the enrollment algorithm
            # says whether this was a real model or the deterministic test
            # double. With EV_E2E_EXPECT_REAL_VOICE=1, a test double fails the
            # e2e run instead of being silently passed. Without it, the round
            # trip is skipped loudly because the hash double is refused outside
            # pytest (docs/FLEET_LAW.md §8).
            if REAL_VOICE_REQUIRED:
                enrollments = http_json(base_url, "GET", "/v1/training/voice/enrollments")
                algorithms = [str(e.get("algorithm")) for e in enrollments]
                if not algorithms:
                    raise RuntimeError(
                        "EV_E2E_EXPECT_REAL_VOICE=1 but no voice enrollment exists"
                    )
                if any(algorithm in TEST_DOUBLE_ALGORITHMS for algorithm in algorithms):
                    raise RuntimeError(
                        "EV_E2E_EXPECT_REAL_VOICE=1 but voiceprint algorithm is a test "
                        f"double: {algorithms}"
                    )
                print(
                    f"ok: voice round trip with real engine "
                    f"(algorithm={algorithms[0]})"
                )
            else:
                print(
                    "skip: voice round trip not proven (EV_E2E_EXPECT_REAL_VOICE=0; "
                    "no real voiceprint provider configured)"
                )

        # Export -> import round-trip (same DB: events dedupe by content hash).
        bundle = Path(tmp) / "bundle.json"
        run_cli(base_url, ["export", "--output", str(bundle)])
        result = run_cli(base_url, ["import", str(bundle), "--mode", "merge"])
        if "imported 0 events" not in result.stdout or "skipped" not in result.stdout:
            raise RuntimeError(f"import round-trip failed:\n{result.stdout}")
        print("ok: ev export -> import round-trip")

        # Onboarding full flow: owner + recovery codes + consent + first memories.
        result = run_cli(
            base_url,
            [
                "onboarding",
                "First: e2e goal",
                "--owner",
                "E2E User",
                "--consent",
                "voice_enrollment",
            ],
        )
        for expected in (
            "owner ",
            "consent granted: voice_enrollment",
            "EV remembers 1 things.",
        ):
            if expected not in result.stdout:
                raise RuntimeError(
                    f"onboarding failed: missing {expected!r} in:\n{result.stdout}"
                )
        print("ok: ev onboarding (full flow)")

        print(f"\nE2E CLI validation passed ({base_url})")
        return 0
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()


if __name__ == "__main__":
    raise SystemExit(main())
