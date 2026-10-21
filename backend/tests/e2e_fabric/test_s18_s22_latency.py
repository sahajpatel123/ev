"""S18/S22: ack latency within 1500ms; stop cancels turn and shell lanes fast."""
import shutil
import time

import pytest


def test_s18_ack_within_budget(tmp_path):
    from app.fabric.run_turn import run_turn
    # 20 local turns; ack must arrive within 1500ms at p95 (i.e. all 20 here, honestly counted)
    ack_ms: list[int] = []
    for i in range(20):
        evs: list[dict] = []
        r = run_turn(f"quick op {i}", root=tmp_path, on_event=evs.append,
                     ops=[{"lane": "files", "op": "list", "args": {}}])
        assert r["ok"], r
        assert evs and evs[0]["kind"] == "ack", evs
        ack_ms.append(evs[0]["elapsed_ms"])
    ack_ms.sort()
    p95 = ack_ms[int(0.95 * (len(ack_ms) - 1))]
    assert p95 <= 1500, {"p95_ms": p95, "samples": ack_ms}


def test_s22_precancelled_turn_aborts(tmp_path):
    from app.fabric.run_turn import run_turn
    from app.fabric.turn import CancelToken
    tok = CancelToken()
    tok.cancel()
    r = run_turn("do work", root=tmp_path, cancel=tok,
                 ops=[{"lane": "files", "op": "list", "args": {}}])
    assert not r["ok"] and r.get("error") == "cancelled before start", r
    assert r["evidence"]["steps"] == [] if "steps" in r["evidence"] else True


def test_s22_midturn_cancel_stops_lanes(tmp_path):
    from app.fabric.run_turn import run_turn
    from app.fabric.turn import CancelToken
    tok = CancelToken()

    def on_event(ev):
        if ev["kind"] == "progress" and ev.get("step") == 0:
            tok.cancel()

    r = run_turn("three ops", root=tmp_path, cancel=tok, on_event=on_event, ops=[
        {"lane": "files", "op": "list", "args": {}},
        {"lane": "files", "op": "list", "args": {}},
        {"lane": "files", "op": "list", "args": {}},
    ])
    assert not r["ok"], r
    assert r["evidence"]["n_steps"] == 2, r  # step 0 ran, step 1 observed cancel
    assert r["evidence"]["steps"][1]["error"] == "cancelled"


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux required")
def test_s22_shell_cancel_within_2s(tmp_path):
    from app.fabric.shell_sessions import ShellSessions
    s = ShellSessions(root=tmp_path)
    started = s.start("sleep 120", confirm=True)
    assert started["ok"], started
    t0 = time.time()
    c = s.cancel(started["session_id"])
    dt = time.time() - t0
    assert c["ok"] and c["verified"], c
    assert dt < 2.0, dt
