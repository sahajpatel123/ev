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


def test_cost_skip_without_provider(monkeypatch):
    import asyncio

    from app.growth import keys as keys_mod
    from app.growth.recurring import cost
    monkeypatch.setattr(cost, "_ledger_24h", lambda: asyncio.sleep(0, result=0.0))
    monkeypatch.setattr(cost, "_live_key", lambda: "")
    monkeypatch.setattr(keys_mod, "load_eval_key", lambda: "")
    res = cost.run()
    assert res["suite"] == "rec-cost"
    assert res["skipped"] == 2 and res["failed"] == 0


def test_cost_flags_big_gap(monkeypatch, tmp_path):
    import asyncio
    import sqlite3

    from app.growth import keys as keys_mod
    from app.growth.recurring import cost
    monkeypatch.setattr(cost, "_ledger_24h", lambda: asyncio.sleep(0, result=1.00))
    monkeypatch.setattr(cost, "_live_key", lambda: "live-key")
    monkeypatch.setattr(keys_mod, "load_eval_key", lambda: "eval-key")
    monkeypatch.setattr(cost, "_key_usage", lambda key: 1.00)
    db = sqlite3.connect(tmp_path / "cost.db")
    db.executescript(cost.SCHEMA)
    db.execute("INSERT INTO cost_baseline(key_role, ts, usage, ledger) VALUES(?,?,?,?)",
               ("live", "t", 0.50, 0.50))
    db.execute("INSERT INTO cost_baseline(key_role, ts, usage, ledger) VALUES(?,?,?,?)",
               ("eval", "t", 0.50, 0.50))
    db.commit()
    db.close()
    dbpath = tmp_path / "cost.db"
    monkeypatch.setattr(cost, "_db", lambda: sqlite3.connect(dbpath))
    res = cost.run()
    assert res["failed"] == 2  # both roles flagged


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
