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


def test_s16_notes_permission_fix_text(tmp_path, monkeypatch):
    from app.fabric import bridge
    monkeypatch.setattr(bridge.shutil, "which", lambda *_a, **_k: None)
    b = bridge.Bridge(root=tmp_path)
    # notes/reminders lanes always carry a fix path when they cannot act
    assert "Automation" in bridge.FIX_STEPS["jxa"]
    assert "Reminders" in bridge.FIX_STEPS["reminders"]
