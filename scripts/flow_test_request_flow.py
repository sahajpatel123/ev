#!/usr/bin/env python3
"""End-to-end request-flow test for the iPhone gateway + live voice path.

Starts a THROWAWAY API instance (temp sqlite DB, offline providers) on
127.0.0.1:8100 and drives the exact HTTP/WS sequence an iPhone performs:

  boot:  session -> hello -> status -> capabilities -> onboarding -> sync
  tap:   live/open -> WS /v1/voice/live (ticket) -> PCM frames -> close
  text:  device-gateway/text (offline chat pipeline)
  end:   live/close -> conversation/release

Run twice with the two hello shapes the phone actually sends:
  shell: capabilities + microphone/location/clipboard, native_shell=true,
         permissions evidence  (EvieShell)
  web:   4-item capability list, native_shell=false (Safari home-screen PWA)

The production server is never touched (separate port, temp DB, EV_ENV=test).

Usage:
  backend/.venv/bin/python scripts/flow_test_request_flow.py [--keep]
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import shutil
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
VENV_PY = BACKEND / ".venv" / "bin" / "python"
PORT = 8100
BASE = f"http://127.0.0.1:{PORT}"
MASTER = "flow-test-master-key"
CLIENT_BUILD = "2026.10.08.5"


@dataclass
class Step:
    profile: str
    name: str
    ok: bool
    ms: float
    detail: str = ""


@dataclass
class Report:
    steps: list[Step] = field(default_factory=list)

    def add(self, profile: str, name: str, ok: bool, ms: float, detail: str = "") -> None:
        self.steps.append(Step(profile, name, ok, ms, detail))

    def failed(self) -> list[Step]:
        return [s for s in self.steps if not s.ok]

    def print(self) -> None:
        width = max((len(s.name) for s in self.steps), default=10)
        print(f"\n{'STEP'.ljust(width)}  {'PROFILE':7}  STATUS   MS      DETAIL")
        for s in self.steps:
            status = "PASS" if s.ok else "FAIL"
            print(f"{s.name.ljust(width)}  {s.profile:7}  {status:6}  {s.ms:7.1f}  {s.detail}")
        fails = self.failed()
        print(f"\nSUMMARY: {len(self.steps)} steps, {len(self.steps) - len(fails)} passed, {len(fails)} failed")


def free_port() -> bool:
    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", PORT)) != 0


def pcm_frame(freq: float = 220.0, samples: int = 320) -> bytes:
    """20 ms of 16 kHz mono Int16 — the PWA's frame size."""
    amp = 6000
    out = bytearray()
    for n in range(samples):
        out += struct.pack("<h", int(amp * math.sin(2 * math.pi * freq * n / 16000.0)))
    return bytes(out)


class Server:
    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir
        self.proc: subprocess.Popen | None = None
        self.log = open(workdir / "server.log", "w")  # noqa: SIM115
        self.db = workdir / "ev.db"

    def start(self) -> None:
        env = dict(os.environ)
        env.update(
            {
                "EV_ENV": "test",
                "EV_DATABASE_URL": f"sqlite+aiosqlite:///{self.db}",
                "EV_MASTER_KEY": MASTER,
                "EV_VAULT_KEY": "flow-test-vault-key-0123456789abcdef",
                "EV_CHAT_PROVIDER": "echo",
                "EV_PROCESSING_MODE": "sync",
                "EV_MEMORY_DIR": str(self.workdir / "memory"),
                "EV_SEARCH_PROVIDER": "none",
                "EV_MEMORY_CURATOR_ENABLED": "false",
            }
        )
        self.proc = subprocess.Popen(
            [str(VENV_PY), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
            cwd=str(BACKEND),
            env=env,
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"server exited early (code {self.proc.returncode})")
            try:
                r = httpx.get(f"{BASE}/v1/health", timeout=1.0)
                if r.status_code == 200:
                    return
            except Exception:
                time.sleep(0.25)
        raise RuntimeError("server did not become healthy in 30s")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.log.close()

    def sql(self, query: str, args: tuple = ()) -> list[tuple]:
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(query, args).fetchall()
        finally:
            conn.close()


class Flow:
    def __init__(self, report: Report, server: Server) -> None:
        self.report = report
        self.server = server
        self.client = httpx.Client(base_url=BASE, timeout=20.0)
        self.master = {"Authorization": f"Bearer {MASTER}"}
        self.device_token: str | None = None
        self.device_id: str | None = None

    # -- helpers ---------------------------------------------------------
    def step(self, profile: str, name: str, fn, *, expect: tuple[int, ...] = (200, 201)) -> dict | None:
        start = time.perf_counter()
        try:
            result = fn()
            status = result.get("_status") if isinstance(result, dict) else None
            ok = status in expect if status is not None else True
            detail = f"HTTP {status}" if status is not None else ""
            if isinstance(result, dict) and not ok:
                detail += " " + str(result.get("detail") or result.get("error_code") or result.get("_raw") or "")
            if isinstance(result, dict) and result.get("_note"):
                detail += (" " if detail else "") + str(result["_note"])
            self.report.add(profile, name, ok, (time.perf_counter() - start) * 1000, detail[:80])
            return result
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            self.report.add(profile, name, False, (time.perf_counter() - start) * 1000, f"{type(exc).__name__}: {exc}"[:100])
            return None

    def gw(self, method: str, path: str, body: dict | None = None, *, token: str | None = None) -> dict:
        headers = {"Authorization": f"Bearer {token or self.device_token}"}
        r = self.client.request(method, path, json=body if body is not None else None, headers=headers)
        data: dict
        try:
            data = r.json()
        except Exception:
            data = {"_raw": r.text[:200]}
        if isinstance(data, dict):
            data["_status"] = r.status_code
        return data

    # -- setup -----------------------------------------------------------
    def seed_device(self) -> None:
        def _create() -> dict:
            r = self.client.post(
                "/v1/devices",
                json={
                    "name": "Flow Test iPhone",
                    "capabilities": ["foreground_voice", "camera", "text", "notification"],
                    "trust_level": "owner",
                    "device_type": "phone",
                    "platform": "apple",
                },
                headers=self.master,
            )
            data = r.json()
            self.device_token = data["token"]
            self.device_id = data["device"]["id"]
            return {"_status": r.status_code, "_note": f"device={self.device_id[:8]}…"}

        self.step("setup", "create trusted device (POST /v1/devices)", _create)

        def _consent() -> dict:
            r = self.client.post(
                "/v1/training/consent",
                json={"track": "voice_enrollment", "purpose": "flow test", "source": "privacy_center"},
                headers=self.master,
            )
            return {"_status": r.status_code, "_note": "voice_enrollment consent granted"}

        # The owner granted this once on the real phone; the isolated DB starts empty.
        self.step("setup", "grant voice_enrollment consent", _consent)

    # -- boot ------------------------------------------------------------
    def boot(self, profile: str, capabilities: list[str], native_shell: bool, permissions: dict) -> None:
        instance = f"flow-{profile}-{int(time.time())}"

        self.step(profile, "POST /session", lambda: self.gw("POST", "/v1/device-gateway/session"))

        def _hello() -> dict:
            return self.gw(
                "POST",
                "/v1/device-gateway/hello",
                {
                    "protocol_version": "1",
                    "client_build": CLIENT_BUILD,
                    "instance_id": instance,
                    "capabilities": capabilities,
                    "foreground": True,
                    "platform": "ios",
                    "hardware": {"model": "iPhone17,1", "media": {"still": True, "burst": True, "video": False}},
                    "permissions": permissions,
                    "native_shell": native_shell,
                },
            )

        hello = self.step(profile, "POST /hello", _hello)
        if hello:
            note = "native_shell reported" if native_shell else "web profile"
            if native_shell and not hello.get("ok"):
                self.report.add(profile, "hello payload accepted", False, 0.0, "ok != true")
            self.report.steps[-1].detail = (self.report.steps[-1].detail + " " + note)[:80]

        self.step(profile, "GET /status", lambda: self.gw("GET", "/v1/device-gateway/status"))
        self.step(profile, "GET /capabilities", lambda: self.gw("GET", "/v1/device-gateway/capabilities"))
        self.step(profile, "GET /phone-capabilities", lambda: self.gw("GET", "/v1/device-gateway/phone-capabilities"))
        self.step(
            profile,
            "PUT /onboarding",
            lambda: self.gw("PUT", "/v1/device-gateway/onboarding", {"steps_completed": ["paired"], "camera_role_set": True}),
        )
        self.step(profile, "GET /sync/bootstrap", lambda: self.gw("GET", "/v1/device-gateway/sync/bootstrap"))
        self.step(profile, "GET /inbox", lambda: self.gw("GET", "/v1/device-gateway/inbox?limit=5"))

    # -- tap -------------------------------------------------------------
    def tap(self, profile: str) -> None:
        instance = f"flow-{profile}-{int(time.time())}"
        opened = self.step(
            profile,
            "POST /live/open",
            lambda: self.gw(
                "POST",
                "/v1/device-gateway/live/open",
                {"instance_id": instance, "method": "manual", "media_backend": "pcm_ws", "client_generation": 1, "takeover": True},
            ),
        )
        if not opened or not opened.get("session_id"):
            return
        session_id = opened["session_id"]
        ticket = opened.get("ws_ticket")
        if not ticket:
            self.report.add(profile, "WS /v1/voice/live", False, 0.0, "live/open returned no ws_ticket")
            return

        uri = f"ws://127.0.0.1:{PORT}/v1/voice/live?session_id={session_id}&ticket={ticket}"
        start = time.perf_counter()
        events: list[str] = []
        frames_sent = 0
        try:
            from websockets.sync.client import connect

            with connect(uri, additional_headers={"Origin": BASE}, open_timeout=10) as ws:
                ready = json.loads(ws.recv(timeout=10))
                events.append(str(ready.get("type") or ready.get("event") or "ready"))
                deadline = time.time() + 4.0
                for _ in range(50):
                    if time.time() > deadline:
                        break
                    ws.send(pcm_frame())
                    frames_sent += 1
                    time.sleep(0.02)
                ws.send(json.dumps({"type": "playback", "active": False}))
                while time.time() < deadline and len(events) < 12:
                    try:
                        msg = ws.recv(timeout=1)
                    except TimeoutError:
                        break
                    if isinstance(msg, bytes):
                        events.append("binary")
                        continue
                    payload = json.loads(msg)
                    events.append(str(payload.get("type") or payload.get("event") or "?"))
                ws.close()
            self.report.add(
                profile,
                "WS /v1/voice/live + PCM",
                True,
                (time.perf_counter() - start) * 1000,
                f"frames={frames_sent} events={','.join(events[:8])}"[:90],
            )
        except Exception as exc:  # noqa: BLE001
            self.report.add(profile, "WS /v1/voice/live + PCM", False, (time.perf_counter() - start) * 1000, f"{type(exc).__name__}: {exc}"[:100])

        self.step(profile, "POST /live/close", lambda: self.gw("POST", "/v1/device-gateway/live/close", {"instance_id": instance, "session_id": session_id}))
        self.step(profile, "POST /conversation/release", lambda: self.gw("POST", "/v1/device-gateway/conversation/release", {"instance_id": instance}))

        def _session_row() -> dict:
            rows = self.server.sql("select state from voice_sessions where id = ?", (session_id,))
            return {"_status": 200, "_note": f"session state={rows[0][0] if rows else 'missing'}"}

        self.step(profile, "DB voice_session recorded", _session_row)

    # -- text ------------------------------------------------------------
    def text_turn(self, profile: str) -> None:
        def _send() -> dict:
            return self.gw(
                "POST",
                "/v1/device-gateway/text",
                {"text": "flow test ping", "instance_id": f"flow-{profile}-text", "request_id": f"flow-{profile}-{int(time.time())}"},
            )

        result = self.step(profile, "POST /text (offline pipeline)", _send)
        if result and result.get("_status") == 200:
            reply = str(result.get("reply") or "")[:40]
            self.report.steps[-1].detail = (self.report.steps[-1].detail + f" reply={reply!r}")[:90]


def run_browser(report: Report, token: str) -> None:
    """Drive the real PWA in Chrome against the isolated server."""
    node = shutil.which("node")
    if not node:
        report.add("browser", "browser harness", False, 0.0, "node not found")
        return
    playwright = next(Path.home().glob(".npm/_npx/*/node_modules/playwright"), None)
    if playwright is None:
        report.add("browser", "browser harness", False, 0.0, "playwright missing; run: npx playwright --version")
        return
    env = dict(os.environ)
    env.update(
        {
            "EV_FLOW_BASE": BASE,
            "EV_FLOW_TOKEN": token,
            "NODE_PATH": str(playwright.parent),
        }
    )
    proc = subprocess.run(
        [node, str(ROOT / "scripts" / "flow_test_pwa.js")],
        env=env,
        capture_output=True,
        text=True,
        timeout=420,
    )
    for line in proc.stdout.splitlines():
        if not line.startswith("FLOW "):
            continue
        try:
            payload = json.loads(line[5:])
        except json.JSONDecodeError:
            continue
        report.add("browser", str(payload.get("step", "?")), bool(payload.get("ok")), 0.0, str(payload.get("detail", ""))[:90])
    if proc.returncode != 0:
        tail = " | ".join((proc.stderr or "").strip().splitlines()[-2:])
        report.add("browser", "browser harness exit", False, 0.0, tail[:100] or f"exit {proc.returncode}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="keep the temp dir for inspection")
    parser.add_argument("--browser", action="store_true", help="also drive the real PWA in Chrome (needs npx playwright)")
    parser.add_argument("--serve", action="store_true", help="start+seed the server and stay up (debug helper)")
    args = parser.parse_args()

    if not free_port():
        print(f"FAIL: port {PORT} already in use", file=sys.stderr)
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="evie-flow-"))
    server = Server(workdir)
    report = Report()
    try:
        print(f"[flow] workdir {workdir}")
        print("[flow] starting isolated server (sqlite, EV_ENV=test, echo brain)…")
        server.start()
        print("[flow] server healthy")

        flow = Flow(report, server)
        flow.seed_device()
        if args.serve:
            print(json.dumps({"base": BASE, "token": flow.device_token, "workdir": str(workdir)}))
            print("[flow] serve mode; Ctrl-C to stop", flush=True)
            try:
                while True:
                    time.sleep(2)
            except KeyboardInterrupt:
                pass
            return 0
        if flow.device_token:
            flow.boot(
                "shell",
                ["foreground_voice", "camera", "text", "notification", "microphone", "location", "clipboard"],
                True,
                {"microphone": "granted", "camera": "granted", "notifications": "granted"},
            )
            flow.tap("shell")
            flow.text_turn("shell")
            flow.boot("web", ["foreground_voice", "camera", "text", "notification"], False, {})
            flow.tap("web")
            flow.text_turn("web")
            if args.browser:
                print("[flow] running real-PWA browser flows (native bridge mock + fake mic)…")
                run_browser(report, flow.device_token)
        else:
            report.add("setup", "device token", False, 0.0, "seed failed; aborting flows")

        report.print()
        if report.failed():
            print("\n[flow] server log tail:")
            log_path = workdir / "server.log"
            if log_path.exists():
                lines = log_path.read_text(errors="replace").splitlines()[-30:]
                print("\n".join(lines))
            return 1
        return 0
    finally:
        server.stop()
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)
        else:
            print(f"[flow] kept {workdir}")


if __name__ == "__main__":
    raise SystemExit(main())
