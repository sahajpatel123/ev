import contextlib

"""S10/S16: headless bridge — JXA without Evie.app socket; honest permission receipts."""
import shutil

import pytest


def test_s10_bridge_roundtrip_receipt_shape(tmp_path):
    from app.fabric.bridge import Bridge
    b = Bridge(root=tmp_path)
    r = b.roundtrip({"op": "ping"})
    assert r["ok"] and r["verified"]
    assert {"ok", "verified", "evidence", "error", "undo"} <= set(r.keys())


@pytest.mark.skipif(shutil.which("osascript") is None, reason="macOS osascript required")
def test_s10_headless_jxa_action(tmp_path):
    from app.fabric.bridge import Bridge
    b = Bridge(root=tmp_path)
    r = b.run("jxa", 'return "headless-ok"')
    assert r["ok"] and r["verified"], r
    assert r["evidence"]["out"] == "headless-ok"


def test_s10_alias_shim(tmp_path):
    from app.fabric.bridge import Bridge
    b = Bridge(root=tmp_path)
    assert b.resolve_alias("Evie.app") == "Evie"
    assert b.resolve_alias("Other.app") == "Other.app"


def test_s16_missing_tool_honest_receipt(tmp_path, monkeypatch):
    from app.fabric import bridge
    monkeypatch.setattr(bridge.shutil, "which", lambda *_a, **_k: None)
    b = bridge.Bridge(root=tmp_path)
    r = b.run("jxa", 'return "x"')
    assert not r["ok"] and not r["verified"], r
    assert r.get("fix") and "Automation" in r["fix"], r


def test_s16_notes_permission_fix_text():
    from app.fabric import bridge
    # notes/reminders lanes always carry a fix path when they cannot act
    assert "Automation" in bridge.FIX_STEPS["jxa"]
    assert "Reminders" in bridge.FIX_STEPS["reminders"]


# --- socket bridge (WP3): app-identity ops with honest fallback ---

import json as _json
import shutil as _shutil
import socket as _socket
import tempfile as _tempfile
import threading as _threading


class _StubBridge:
    def __init__(self, *, jxa_ok=True, shot_bytes=b"P" * 4096):
        self.dir = __import__("pathlib").Path(_tempfile.mkdtemp(prefix="sb"))
        self.sock = self.dir / "b.sock"
        self.token_file = self.dir / "b.token"
        self.token = "stub-" + "y" * 40
        self.token_file.write_text(self.token)
        self.jxa_ok = jxa_ok
        self.shot_bytes = shot_bytes

    def start(self):
        server = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        server.bind(str(self.sock))
        server.listen(8)
        server.settimeout(5.0)
        self._server = server

        def serve():
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
                        req = _json.loads(data.decode().strip().split("\n", 1)[0])
                    except ValueError:
                        continue
                    out = {"id": req.get("id")}
                    if req.get("token") != self.token:
                        out.update(ok=False, error="bad_token")
                    elif req.get("op") == "jxa":
                        out.update({"ok": True, "result": {"out": "Finder"}} if self.jxa_ok
                                   else {"ok": False, "error": "automation_denied",
                                         "fix": "System Settings > Privacy & Security > Automation > Evie."})
                    elif req.get("op") == "screenshot":
                        p = self.dir / "shot.png"
                        p.write_bytes(self.shot_bytes)
                        out.update(ok=True, result={"path": str(p), "sha256": "s",
                                                   "width": 8, "height": 8})
                    elif req.get("op") == "ping":
                        out.update(ok=True, result={"op": "ping"})
                    else:
                        out.update(ok=False, error="unknown_op")
                    conn.sendall((_json.dumps(out) + "\n").encode())

        _threading.Thread(target=serve, daemon=True).start()

    def stop(self):
        with contextlib.suppress(Exception):
            self._server.close()
        _shutil.rmtree(self.dir, ignore_errors=True)


def _patch_bridge(monkeypatch, stub):
    monkeypatch.setattr("app.fabric.bridge_client.socket_path", lambda: stub.sock)
    monkeypatch.setattr("app.fabric.bridge_client.token_path", lambda: stub.token_file)


def test_s10_tell_app_routes_to_bridge(tmp_path, monkeypatch):
    from app.fabric.bridge import Bridge
    stub = _StubBridge()
    stub.start()
    try:
        _patch_bridge(monkeypatch, stub)
        r = Bridge(root=tmp_path).run("jxa", 'tell application "Finder" to return name')
        assert r["ok"] and r["verified"], r
        assert r["evidence"]["out"] == "Finder"
    finally:
        stub.stop()


def test_s10_trivial_jxa_stays_headless(tmp_path, monkeypatch):
    import shutil as _sh
    if _sh.which("osascript") is None:
        pytest.skip("macOS osascript required")
    from app.fabric.bridge import Bridge
    monkeypatch.setattr("app.fabric.bridge_client.socket_path",
                        lambda: tmp_path / "no.sock")
    r = Bridge(root=tmp_path).run("jxa", 'return "headless-ok"')
    assert r["ok"] and r["verified"], r


def test_s10_bridge_down_honest(tmp_path, monkeypatch):
    from app.fabric.bridge import Bridge
    monkeypatch.setattr("app.fabric.bridge_client.socket_path",
                        lambda: tmp_path / "no.sock")
    b = Bridge(root=tmp_path)
    r = b.run("jxa", 'tell application "Finder" to return 1')
    assert not r["ok"]
    assert "bridge" in str(r.get("error", "")).lower() and r.get("fix"), r
    s = b.screenshot()
    assert not s["ok"] and "bridge" in str(s.get("error", "")).lower(), s


def test_s10_screenshot_roundtrip(tmp_path, monkeypatch):
    from app.fabric.bridge import Bridge
    stub = _StubBridge()
    stub.start()
    try:
        _patch_bridge(monkeypatch, stub)
        r = Bridge(root=tmp_path).screenshot()
        assert r["ok"] and r["verified"], r
        assert r["evidence"]["width"] == 8
    finally:
        stub.stop()


def test_s16_automation_denied_fix_text_from_bridge(tmp_path, monkeypatch):
    from app.fabric.bridge import Bridge
    stub = _StubBridge(jxa_ok=False)
    stub.start()
    try:
        _patch_bridge(monkeypatch, stub)
        r = Bridge(root=tmp_path).run("jxa", 'tell application "Finder" to return 1')
        assert not r["ok"]
        assert r.get("error") == "automation_denied", r
        assert "Automation" in (r.get("fix") or ""), r
    finally:
        stub.stop()


def test_s16_bridge_bad_token_rejected(tmp_path, monkeypatch):
    from app.fabric.bridge_client import BridgeClient
    stub = _StubBridge()
    stub.start()
    try:
        monkeypatch.setattr("app.fabric.bridge_client.socket_path", lambda: stub.sock)
        monkeypatch.setattr("app.fabric.bridge_client.token_path",
                            lambda: stub.dir / "wrong.token")
        (stub.dir / "wrong.token").write_text("wrong-" + "z" * 40)
        r = BridgeClient(timeout=5).call("ping")
        assert not r["ok"]
        assert r.get("error") == "bad_token", r
    finally:
        stub.stop()
