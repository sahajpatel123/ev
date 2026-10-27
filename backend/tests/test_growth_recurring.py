"""GE8 recurring checks: shell drill, soak, idle, cost, egress, driver."""

from __future__ import annotations


def test_shell_drill_real():
    from app.growth.recurring.shell_drill import run
    res = run()
    assert res["suite"] == "rec-shell-drill"
    assert res["status"] != "blocked", res["coverage_note"]
    assert res["failed"] == 0, res["failures"]
    assert res["n"] == 5


def test_soak_smoke_fast():
    import socket
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 18005))
    except OSError:
        import pytest
        pytest.skip("port 18005 busy")
    finally:
        s.close()
    from app.growth.recurring.soak import run
    res = run(minutes=0.1, workload_s=5)
    assert res["suite"] == "rec-soak"
    assert res["status"] != "blocked", res["coverage_note"]
    assert res["failed"] == 0, res["failures"]
    assert "recovery-1" in res["coverage_note"]


def test_idle_verdict_logic(monkeypatch):
    from app.growth.recurring import idle
    monkeypatch.setattr(idle, "_sample", lambda patterns: {
        "samples": [0.4], "pids": [111], "load1": 1.0, "cores": 8,
        "mean": 0.4, "max": 0.4})
    ok = idle.run_app()
    assert ok["failed"] == 0 and ok["n"] == 1
    monkeypatch.setattr(idle, "_sample", lambda patterns: {
        "samples": [9.9], "pids": [111], "load1": 99.0, "cores": 8,
        "mean": 9.9, "max": 9.9})
    bad = idle.run_backend()
    assert bad["failed"] == 1
    assert "MACHINE BUSY" in bad["coverage_note"]


def _cost_db(tmp_path, rows=()):
    import sqlite3

    from app.growth.recurring import cost
    db = sqlite3.connect(tmp_path / "cost.db")
    db.executescript(cost.SCHEMA)
    for role, ts, usage in rows:
        db.execute("INSERT INTO cost_snapshot(key_role, ts, usage) VALUES(?,?,?)",
                   (role, ts, usage))
    db.commit()
    db.close()
    return tmp_path / "cost.db"


def test_cost_skip_without_provider(monkeypatch):
    import asyncio

    from app.growth import keys as keys_mod
    from app.growth.recurring import cost
    monkeypatch.setattr(cost, "_ledger_window", lambda since: asyncio.sleep(0, result=0.0))
    monkeypatch.setattr(cost, "_live_key", lambda: "")
    monkeypatch.setattr(keys_mod, "load_eval_key", lambda: "")
    res = cost.run()
    assert res["suite"] == "rec-cost"
    assert res["skipped"] == 2 and res["failed"] == 0
    assert res["status"] == "partial"  # all-skip is never "ok"


def test_cost_flags_big_gap_and_advances_snapshot(monkeypatch, tmp_path):
    import asyncio
    import sqlite3
    from datetime import UTC, datetime, timedelta

    from app.growth import keys as keys_mod
    from app.growth.recurring import cost
    monkeypatch.setattr(cost, "_ledger_window", lambda since: asyncio.sleep(0, result=1.00))
    monkeypatch.setattr(cost, "_live_key", lambda: "live-key")
    monkeypatch.setattr(keys_mod, "load_eval_key", lambda: "eval-key")
    monkeypatch.setattr(cost, "_key_usage", lambda key: 1.00)
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    dbpath = _cost_db(tmp_path, [("live", old, 0.50), ("eval", old, 0.50)])
    monkeypatch.setattr(cost, "_db", lambda: sqlite3.connect(dbpath))
    res = cost.run()
    assert res["failed"] == 2  # both roles flagged
    db = sqlite3.connect(dbpath)
    rows = {r[0]: r[1] for r in db.execute("SELECT key_role, usage FROM cost_snapshot")}
    assert rows == {"live": 1.00, "eval": 1.00}  # snapshot advanced past the judged window


def test_cost_first_run_baselines_and_short_window_skips(monkeypatch, tmp_path):
    import asyncio
    import sqlite3
    from datetime import UTC, datetime, timedelta

    from app.growth import keys as keys_mod
    from app.growth.recurring import cost
    monkeypatch.setattr(cost, "_ledger_window", lambda since: asyncio.sleep(0, result=0.0))
    monkeypatch.setattr(cost, "_live_key", lambda: "live-key")
    monkeypatch.setattr(cost, "_key_usage", lambda key: 0.0)
    monkeypatch.setattr(keys_mod, "load_eval_key", lambda: "")
    dbpath = _cost_db(tmp_path)
    monkeypatch.setattr(cost, "_db", lambda: sqlite3.connect(dbpath))
    res = cost.run()
    assert res["skipped"] == 2 and "baseline established" in res["coverage_note"]
    res2 = cost.run()  # 0h-old window: too short to judge
    assert res2["skipped"] == 2 and "too short" in res2["coverage_note"]
    # Age the snapshot 2h with no spend anywhere: honest no-spend skip.
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    db = sqlite3.connect(dbpath)
    db.execute("UPDATE cost_snapshot SET ts=? WHERE key_role='live'", (old,))
    db.commit()
    db.close()
    monkeypatch.setattr(cost, "_key_usage", lambda key: 0.0)
    res3 = cost.run()
    assert "no spend" in res3["coverage_note"]


def test_egress_structure(monkeypatch):
    from app.growth.recurring import egress
    rows = [{"data_class": "memory", "cost_usd": 0.01, "cost_source": "usage"},
            {"data_class": None, "cost_usd": None, "cost_source": None}]
    import asyncio
    monkeypatch.setattr(egress, "_rows_24h", lambda: asyncio.sleep(0, result=rows))
    res = egress.run()
    assert res["n"] == 2 and res["failed"] == 1
    assert "FIX_REQUEST" in res["coverage_note"] or "present" in res["coverage_note"]


def test_driver_rejects_unknown():
    import pytest

    from app.growth.recurring.driver import main
    with pytest.raises(SystemExit):
        main(["nope-not-a-check"])


def test_egress_empty_ledger_skips_not_passes(monkeypatch):
    import asyncio

    from app.growth.recurring import egress
    monkeypatch.setattr(egress, "_rows_24h", lambda: asyncio.sleep(0, result=[]))
    res = egress.run()
    assert res["status"] == "partial"
    assert (res["n"], res["passed"], res["failed"], res["skipped"]) == (1, 0, 0, 1)


def test_soak_fds_none_on_dead_group():
    import subprocess

    from app.growth.recurring.soak import _fds
    proc = subprocess.Popen(["true"])
    proc.wait()
    assert _fds(proc) is None  # dead group: unmeasured, never 0


def test_gate_diff_fails_closed_on_bad_ref():
    import pytest

    import app.growth.train.gate as G
    with pytest.raises(ValueError):
        G.diff_files("main", "nonexistent-branch-xyz-123")


def test_newer_than_empty_pin_reports_nothing():
    from app.growth.modelwatch.watch import newer_than
    assert newer_than("", ["muse-spark-9.9", "gemini-3.8-live-x"], "spark") == []
