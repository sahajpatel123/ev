"""Receipts, recorder, runtime fake, and the GUI lock."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.fabric.skills import guilock
from app.fabric.skills.receipts import CONTRACT, Recorder, error, fail, receipt
from app.fabric.skills.runtime import Proc, applescript_quote, as_list
from app.fabric.skills.testing import FakeRuntime

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "gui-lock"


def test_receipt_clamps_verified_without_ok():
    r = receipt(False, verified=True)
    assert r["ok"] is False and r["verified"] is False
    assert r["contract"] == CONTRACT


def test_error_codes_are_stable_and_unknown_maps_to_internal():
    assert error("not_found", "x")["code"] == "not_found"
    assert error("something_new", "x")["code"] == "internal"


def test_fail_carries_an_honest_say():
    r = fail("locked", "The screen is locked.")
    assert r["say"] == "The screen is locked."
    assert r["error"]["code"] == "locked" and r["ok"] is False


def test_recorder_verified_only_after_verify_step():
    t = iter(range(0, 1000))
    rec = Recorder("x.y", clock=lambda: next(t) / 1000)
    rec.add("act", "do", True, channel="dom")
    assert rec.finish(True)["verified"] is False  # an act alone never verifies
    rec.add("verify", "check", True)
    r = rec.finish(True)
    assert r["verified"] is True and r["channel"] == "dom"
    assert r["timings_ms"]["first_effect"] >= 0
    assert [s["n"] for s in r["steps"]] == [1, 2]


def test_recorder_failed_verify_never_verifies():
    rec = Recorder("x.y")
    rec.add("verify", "check", False)
    assert rec.finish(True)["verified"] is False


def test_recorder_context_step_times_and_records_failure():
    rec = Recorder("x.y")
    with rec.step("perceive", "probe", "ax") as s:
        s["ok"] = True
        s["evidence"] = {"n": 3}
    assert rec.steps[0]["evidence"] == {"n": 3} and rec.steps[0]["channel"] == "ax"


def test_fake_runtime_rules_and_virtual_clock():
    rt = FakeRuntime()
    rt.when(r"count of windows", "3")
    p = rt.osascript('tell application "Ghostty" to count of windows')
    assert p.ok and p.out == "3"
    miss = rt.osascript("nothing matches")
    assert not miss.ok
    t0 = rt.monotonic()
    rt.sleep(2.5)
    assert rt.monotonic() - t0 == 2.5
    assert rt.ran("Ghostty")


def test_proc_permission_and_not_running_detection():
    assert Proc(False, err="execution error: Not authorized to send Apple events (-1743)").permission_denied
    assert Proc(False, err="Application isn't running. (-600)").app_not_running
    assert not Proc(False, err="syntax error").permission_denied


def test_quote_and_list_helpers():
    assert applescript_quote('a"b\\c') == '"a\\"b\\\\c"'
    assert as_list("a, b, c") == ["a", "b", "c"]


@pytest.fixture
def lockdir(tmp_path, monkeypatch):
    monkeypatch.setenv("EV_HANDS_LOCK_DIR", str(tmp_path))
    guilock._mod = None  # reload honours the env var per call; no state kept
    return tmp_path


def _script(*args, env_dir):
    env = dict(os.environ, EV_HANDS_LOCK_DIR=str(env_dir))
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True,
                          text=True, env=env)


def test_lock_is_exclusive_and_owner_checked(lockdir):
    a = _script("acquire", "--owner", "CLI4", "--pid", str(os.getpid()), env_dir=lockdir)
    assert a.returncode == 0
    b = _script("acquire", "--owner", "CLI1", env_dir=lockdir)
    assert b.returncode == 1 and "CLI4" in b.stderr
    wrong = _script("release", "--owner", "CLI1", env_dir=lockdir)
    assert wrong.returncode == 1
    st = _script("status", "--json", env_dir=lockdir)
    assert '"held": true' in st.stdout
    assert _script("release", "--owner", "CLI4", env_dir=lockdir).returncode == 0
    assert _script("status", env_dir=lockdir).stdout.strip() == "free"


def test_lock_stale_only_when_dead_and_old(lockdir):
    gui = lockdir / "gui"
    gui.mkdir()
    dead = 2_000_000_000  # no such pid
    old = 1_000_000_000
    (gui / "owner").write_text(f"owner=CLI9\npid={dead}\nstarted_epoch={old}\npurpose=x\n")
    st = guilock._load()
    info = st.status()
    assert info["stale"] is True and info["pid_alive"] is False
    # a live pid, however old, is never stale
    (gui / "owner").write_text(f"owner=CLI9\npid={os.getpid()}\nstarted_epoch={old}\n")
    assert st.status()["stale"] is False and st.status()["over_hold_limit"] is True
    # a dead pid younger than 30 minutes is not stale either
    import time

    (gui / "owner").write_text(f"owner=CLI9\npid={dead}\nstarted_epoch={time.time() - 60}\n")
    assert st.status()["stale"] is False


def test_lock_breaks_stale_and_logs_it(lockdir):
    gui = lockdir / "gui"
    gui.mkdir()
    (gui / "owner").write_text("owner=CLI9\npid=2000000000\nstarted_epoch=1000000000\n")
    with guilock.gui_lock("CLI4", "unit"):
        assert guilock.status()["owner"] == "CLI4"
    log = (lockdir / "gui-lock.log").read_text()
    assert "BROKE stale lock owner=CLI9" in log
    assert not (lockdir / "gui").exists()


def test_context_manager_raises_busy_with_holder(lockdir):
    with guilock.gui_lock("CLI1", "first"), pytest.raises(guilock.GuiLockBusy) as exc:
        with guilock.gui_lock("CLI4", "second"):
            pass
    assert exc.value.holder["owner"] == "CLI1"
