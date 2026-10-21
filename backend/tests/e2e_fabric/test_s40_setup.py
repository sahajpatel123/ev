"""S40: `ev setup` idempotency; verifiers report denied honestly."""

from __future__ import annotations

import contextlib
import json
import socket
import threading
from pathlib import Path
from typing import Any

import pytest


class FakeBridge:
    """Stub unix-socket bridge speaking the real protocol (token-checked)."""

    def __init__(self, sock: Path, token_file: Path, *,
                 tcc: dict[str, str] | None = None,
                 jxa_ok: bool = True, shot_bytes: bytes = b"",
                 readable: bool = True):
        self.sock = sock
        self.token = "fake-token-" + "x" * 40
        token_file.write_text(self.token)
        self.tcc = tcc or {}
        self.jxa_ok = jxa_ok
        self.shot_bytes = shot_bytes
        self.readable = readable
        self._thread: threading.Thread | None = None
        self.requests: list[dict] = []

    def start(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.sock))
        server.listen(8)
        server.settimeout(5.0)

        def serve() -> None:
            while True:
                try:
                    conn, _ = server.accept()
                except OSError:
                    return
                with conn:
                    data = b""
                    while b"\n" not in data:
                        piece = conn.recv(65536)
                        if not piece:
                            break
                        data += piece
                    try:
                        req = json.loads(data.decode().strip().split("\n", 1)[0])
                    except ValueError:
                        continue
                    self.requests.append(req)
                    out: dict[str, Any] = {"id": req.get("id")}
                    if req.get("token") != self.token:
                        out.update(ok=False, error="bad_token")
                    else:
                        out.update(self._answer(str(req.get("op") or ""),
                                                dict(req.get("args") or {})))
                    conn.sendall((json.dumps(out) + "\n").encode())

        self._thread = threading.Thread(target=serve, daemon=True)
        self._thread.start()
        self._server = server

    def _answer(self, op: str, args: dict) -> dict:
        if op == "ping":
            return {"ok": True, "result": {"op": "ping"}}
        if op == "tcc_status":
            return {"ok": True, "result": {"permissions": [
                {"permission": name, "status": status, "detail": "fake"}
                for name, status in self.tcc.items()
            ]}}
        if op == "jxa":
            if self.jxa_ok:
                return {"ok": True, "result": {"out": "FakeApp"}}
            return {"ok": False, "error": "automation_denied",
                    "fix": "System Settings > Privacy & Security > Automation > Evie."}
        if op == "screenshot":
            path = self.sock.parent / "fake-shot.png"
            path.write_bytes(self.shot_bytes)
            return {"ok": True, "result": {"path": str(path), "sha256": "x",
                                           "width": 8, "height": 8,
                                           "bytes": len(self.shot_bytes)}}
        if op == "probe_read":
            return {"ok": True, "result": {"path": args.get("path"),
                                           "readable": self.readable}}
        return {"ok": False, "error": f"unknown_op:{op}"}

    def stop(self) -> None:
        with contextlib.suppress(Exception):
            self._server.close()


@pytest.fixture()
def no_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.fabric.bridge_client.socket_path",
                        lambda: tmp_path / "no.sock")
    monkeypatch.setattr("app.fabric.bridge_client.token_path",
                        lambda: tmp_path / "no.token")
    return tmp_path


@pytest.fixture()
def fake_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import shutil
    import tempfile

    # AF_UNIX sun_path caps at 104 chars; pytest tmp paths exceed it, so the
    # stub socket lives in a short TMPDIR dir (the real ~/.ev path is short).
    shortdir = Path(tempfile.mkdtemp(prefix="fb"))
    sock = shortdir / "b.sock"
    token = shortdir / "b.token"
    monkeypatch.setattr("app.fabric.bridge_client.socket_path", lambda: sock)
    monkeypatch.setattr("app.fabric.bridge_client.token_path", lambda: token)
    granted = {name: "granted" for name in (
        "accessibility", "automation", "full_disk_access", "screen_recording",
        "microphone", "input_monitoring", "contacts", "calendars", "reminders")}
    bridge = FakeBridge(sock, token, tcc=granted, shot_bytes=b"P" * 4096)
    bridge.start()
    yield bridge
    bridge.stop()
    shutil.rmtree(shortdir, ignore_errors=True)


def _patch_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "setup-state.json"
    monkeypatch.setattr("clients.cli.setup.state_path", lambda: state)
    return state


def test_s40_report_only_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                         no_bridge) -> None:
    from clients.cli.setup import run_setup

    state = _patch_state(tmp_path, monkeypatch)
    rep = run_setup(interactive=False)
    text = rep.text()
    for section in ("== env:", "== app:", "== tcc:", "== launchd:", "== tailscale:"):
        assert section in text
    assert not state.exists(), "report-only must not write state"


def test_s40_report_only_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                    no_bridge, capsys) -> None:
    from clients.cli.setup import run_setup

    _patch_state(tmp_path, monkeypatch)
    first = run_setup(interactive=False).text()
    capsys.readouterr()
    second = run_setup(interactive=False).text()
    def headers(t):
        return sorted(line for line in t.splitlines() if line.startswith("=="))

    assert headers(first) == headers(second)


def test_s40_bridge_down_never_granted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                       no_bridge) -> None:
    from clients.cli.setup import PERMISSIONS, poll_until

    for perm in PERMISSIONS:
        status, _ = perm.status()
        assert status != "granted", perm.name
        ok, _ = perm.verify()
        assert ok is False, perm.name
    # Polling short-circuits honestly (no hang, no phantom grant).
    status, _ = poll_until(PERMISSIONS[0], timeout_s=0.1, interval_s=0.05)
    assert status != "granted"


def test_s40_fake_bridge_all_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                  fake_bridge) -> None:
    from clients.cli.setup import PERMISSIONS

    for perm in PERMISSIONS:
        ok, detail = perm.verify()
        assert ok is True, (perm.name, detail)


def test_s40_resume_skips_passed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                 no_bridge) -> None:
    from clients.cli.setup import run_setup

    monkeypatch.setattr("clients.cli.setup.open_settings_pane", lambda url: True)
    state = _patch_state(tmp_path, monkeypatch)
    state.write_text(json.dumps({"tcc": {"microphone": "PASS"}}))
    rep = run_setup(interactive=True, poll_s=0, phases=["tcc"])
    assert "already verified (resume skip)" in rep.text()


def test_s40_live_send_skipped_report_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                           no_bridge) -> None:
    from clients.cli.setup import run_setup

    _patch_state(tmp_path, monkeypatch)
    rep = run_setup(interactive=False, phases=["live_send"])
    assert "SKIP" in rep.text()


async def test_s40_live_send_flow_with_fake_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from clients.cli.setup import Report, phase_live_send_async

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    proof = tmp_path / "Desktop/evie-live-proof"
    proof.mkdir(parents=True)
    calls: list[tuple[str, Any]] = []

    class FakeResp:
        def __init__(self, payload, status=200):
            self._payload = payload
            self.status_code = status

        def json(self):
            return self._payload

    class FakeClient:
        async def post(self, path: str, json: dict | None = None):
            calls.append(("POST", path))
            assert path == "/v1/fabric/exec"
            return FakeResp({"result": {"action_id": "act-1"}})

        async def get(self, path: str):
            calls.append(("GET", path))
            return FakeResp({"approvals": []})

    (proof / "setup-ping.txt").write_text("x")
    monkeypatch.setattr("builtins.input", lambda *a: "")
    rep = Report()
    await phase_live_send_async(rep, client_factory=FakeClient)
    assert "PASS" in rep.text()
    assert not (proof / "setup-ping.txt").exists(), "live check must self-clean"


def test_s40_doctor_fabric_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                 no_bridge) -> None:
    from clients.cli.setup import run_doctor

    rep = run_doctor(fabric_only=True)
    text = rep.text()
    for needle in ("tmux", "screen_recording", "launchd:ev.api", "audit chain", "panic"):
        assert needle in text, needle


def test_s40_parser() -> None:
    from clients.cli import build_parser

    parser = build_parser()
    assert parser.parse_args(["setup"]).command == "setup"
    assert parser.parse_args(["setup", "--report-only"]).report_only is True
    assert parser.parse_args(["doctor", "--fabric"]).fabric is True
    assert parser.parse_args(["approvals"]).command == "approvals"
    assert parser.parse_args(["approve", "x"]).action_id == "x"
    assert parser.parse_args(["deny", "x"]).action_id == "x"


async def test_s40_approve_deny_paths() -> None:
    from clients.cli import approvals_pending, approve, deny_approval

    seen: list[tuple[str, str]] = []

    class FakeResp:
        status_code = 200

        def json(self):
            return {"ok": True}

    class FakeClient:
        async def post(self, path: str, json: dict | None = None):
            seen.append(("POST", path))
            return FakeResp()

        async def get(self, path: str):
            seen.append(("GET", path))
            return FakeResp()

    await approvals_pending(client=FakeClient())
    await approve("a1", client=FakeClient())
    await deny_approval("a2", client=FakeClient())
    assert ("GET", "/v1/fabric/approvals/pending") in seen
    assert ("POST", "/v1/fabric/approvals/a1/approve") in seen
    assert ("POST", "/v1/fabric/approvals/a2/deny") in seen
