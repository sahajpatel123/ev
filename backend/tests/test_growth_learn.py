"""GE4: miners, clusters, LIFE cases, review gating (fixtures, tmp DBs)."""

from __future__ import annotations

import json
import sqlite3

import pytest


@pytest.fixture()
def tmp_growth_db(monkeypatch, tmp_path):
    import app.growth.store as S

    db = tmp_path / "g.sqlite"

    def _tmpconnect():
        conn = sqlite3.connect(str(db))
        return conn

    monkeypatch.setattr(S, "_connect", _tmpconnect)
    return db


def test_mine_failed_receipts_and_denials():
    from app.growth.learn import mine as M

    audit = [
        {"lane": "files", "op": "delete", "ok": False, "decision": "EXECUTED", "t": 100.0},
        {"lane": "comms", "op": "send", "ok": False, "decision": "DENY", "t": 101.0},
        {"lane": "files", "op": "read", "ok": True, "decision": "EXECUTED", "t": 102.0},
    ]
    got = M.mine_failed_receipts(audit)
    assert sorted(g["source"] for g in got) == ["denied", "failed-receipt"]


def test_mine_undos_match_target_within_window():
    from app.growth.learn import mine as M

    audit = [
        {"lane": "files", "op": "delete", "ok": True, "t": 100.0, "target_hash": "ab"},
        {"lane": "files", "op": "undo", "ok": True, "t": 200.0, "target_hash": "ab"},
        {"lane": "files", "op": "undo", "ok": True, "t": 99999.0, "target_hash": "zz"},
    ]
    got = M.mine_undos(audit)
    assert len(got) == 2 and "delete" in got[0]["receipts"]


def test_mine_ledger_corrections_rephrases_cancels():
    from app.growth.learn import mine as M

    turns = [
        {"owner": "delete the report", "evie": "done", "kind": "done", "at": "2026-10-27T10:00:00+00:00"},
        {"owner": "delete the report file", "evie": "?", "kind": "done", "at": "2026-10-27T10:00:30+00:00"},
        {"owner": "no, not that one", "evie": "?", "kind": "cancelled", "at": "2026-10-27T10:01:00+00:00"},
    ]
    got = M.mine_ledger(turns)
    sources = sorted(g["source"] for g in got)
    assert "correction" in sources and "cancelled" in sources and "rephrase" in sources


def test_queue_record_round_trip(tmp_growth_db):
    from app.growth.learn import queue as Q

    fid = Q.record("ev-bad", utterance="that was wrong", prov="owner")
    assert fid > 0
    rows = Q.list_recent(10)
    assert len(rows) == 1 and rows[0]["source"] == "ev-bad"
    assert Q.count_by_source(7) == {"ev-bad": 1}
    assert Q.count_by_prov() == {"owner": 1}
    Q.record("failed-receipt", receipts="x")  # default prov is unstamped
    assert len(Q.list_recent(10)) == 1  # owner-only default hides it
    assert len(Q.list_recent(10, prov=None)) == 2
    assert Q.count_by_prov() == {"owner": 1, "unstamped": 1}


def test_queue_migration_adds_prov(tmp_growth_db):
    import sqlite3

    from app.growth.learn import queue as Q

    db = tmp_growth_db
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE failures(id INTEGER PRIMARY KEY, ts TEXT NOT NULL, source TEXT NOT NULL,"
                 " utterance TEXT NOT NULL DEFAULT '', trace_ref TEXT NOT NULL DEFAULT '',"
                 " policy TEXT NOT NULL DEFAULT '', receipts TEXT NOT NULL DEFAULT '',"
                 " world TEXT NOT NULL DEFAULT '', cluster_id TEXT NOT NULL DEFAULT '')")
    conn.execute("INSERT INTO failures(ts, source) VALUES('t', 'failed-receipt')")
    conn.commit()
    conn.close()
    assert Q.count_by_prov() == {"unstamped": 1}  # migration ran, row kept
    Q.record("ev-bad", prov="owner")
    assert Q.count_by_prov() == {"unstamped": 1, "owner": 1}


def test_provenance_stamp_and_partition(monkeypatch, tmp_path):
    import app.utils.provenance as P

    monkeypatch.delenv("EV_GROWTH_EVAL", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(P, "drill_window_path", lambda: tmp_path / "w.json")
    assert P.stamp() == "owner"
    assert P.is_owner("owner") and not P.is_owner("suite")
    assert not P.is_owner(None) and not P.is_owner("")
    monkeypatch.setenv("EV_GROWTH_EVAL", "1")
    assert P.stamp() == "suite"
    monkeypatch.delenv("EV_GROWTH_EVAL")
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x")
    assert P.stamp() == "pytest"
    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    P.write_drill_window(60, "test drill")
    assert P.stamp() == "live-proof"
    P.clear_drill_window()
    assert P.stamp() == "owner"
    assert P.stamp(explicit="suite") == "suite"


def test_partition_gate_fail_closed():
    from app.growth.learn import mine as M

    rows = [{"src": "owner", "v": 1}, {"src": "suite"}, {"src": "pytest"},
            {"src": "live-proof"}, {"v": 2}, {"src": ""}]
    owner, excluded = M.partition(rows)
    assert [r["v"] for r in owner if "v" in r] == [1]
    assert excluded == {"suite": 1, "pytest": 1, "live-proof": 1, "unstamped": 2}


def test_miners_ingest_owner_only_after_partition():
    from app.growth.learn import mine as M

    audit = [
        {"lane": "f", "op": "d", "ok": False, "decision": "X", "t": 1.0, "src": "owner"},
        {"lane": "f", "op": "d", "ok": False, "decision": "X", "t": 2.0, "src": "suite"},
        {"lane": "f", "op": "d", "ok": False, "decision": "X", "t": 3.0},
    ]
    owner, excluded = M.partition(audit)
    got = M.mine_failed_receipts(owner)
    assert len(got) == 1
    assert excluded == {"suite": 1, "unstamped": 1}


def test_audit_record_stamps_src_outside_hash(tmp_path, monkeypatch):
    from app.config import settings
    from app.fabric import audit as A

    monkeypatch.setattr(settings, "fabric_audit_file", str(tmp_path / "a.jsonl"), raising=False)
    assert "PYTEST_CURRENT_TEST" in __import__("os").environ  # harness marker live
    A.record("files", "read", target="t", decision="EXECUTED", ok=True)
    rows = M_read(tmp_path / "a.jsonl")
    assert rows[0]["src"] == "pytest"
    A.record("files", "read", target="t", decision="EXECUTED", ok=True, src="owner")
    assert M_read(tmp_path / "a.jsonl")[1]["src"] == "owner"
    assert A.verify()["ok"] is True  # chain still verifies with the new field


def M_read(path):
    import json

    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def test_remember_exchange_stamps_src(monkeypatch, tmp_path):
    import app.cognitive.session_store as SS

    monkeypatch.setattr(SS, "_path", lambda: tmp_path / "session.json")
    SS.reset_for_tests()
    from app.cognitive.intent import remember_exchange

    s = SS.current()
    remember_exchange(s, owner="hi", assistant="hello", kind="done")
    rows = SS.current().recent_turns
    assert rows and rows[-1]["src"] == "pytest"  # harness marker live in tests


def test_quarantine_flips_pending_only(tmp_growth_db):
    from app.growth.learn import candidates as C

    a = C.draft("c1", "t1", {}, {})
    b = C.draft("c2", "t2", {}, {})
    C.set_status(a, "approved")
    assert C.quarantine_unprovenanced() == 1
    assert [c["status"] for c in C.pending()] == []
    from app.growth.learn.candidates import _db

    conn = _db()
    try:
        st = {r[0]: r[1] for r in conn.execute("SELECT id, status FROM candidates")}
    finally:
        conn.close()
    assert st == {a: "approved", b: "quarantined"}


def test_cluster_and_draft(tmp_growth_db):
    from app.growth.learn import candidates as C
    from app.growth.learn import cluster as CL

    fails = [
        {"source": "failed-receipt", "receipts": "files/delete ok=false", "policy": "EXECUTED", "ts": "t1"},
        {"source": "failed-receipt", "receipts": "files/delete ok=false", "policy": "EXECUTED", "ts": "t2"},
        {"source": "correction", "receipts": "", "policy": "done", "ts": "t3"},
    ]
    groups = CL.cluster(fails)
    assert len(groups) == 2
    ranked = CL.rank(groups)
    assert len(ranked[0][1]) == 2
    cid = C.draft_from_cluster(ranked[0][0], ranked[0][1])
    pend = C.pending()
    assert len(pend) == 1 and pend[0]["id"] == cid
    assert json.loads(pend[0]["oracle"])["verdict"] == "PENDING-REVIEW"
    C.set_status(cid, "approved")
    assert C.pending() == []


def test_life_cases_run_pass_fail_skip(tmp_growth_db):
    from app.growth.learn import life_cases as L

    ok_id = L.add(1, "create works", "files", "create",
                  {"call": {"args": ["note.txt", "hi"], "kwargs": {}}}, {"ok": True})
    bad_id = L.add(1, "read missing fails", "files", "read",
                   {"call": {"args": ["nope.txt"], "kwargs": {}}}, {"ok": True})
    skip_id = L.add(1, "send parked", "comms", "send", {}, {"ok": True})
    rep = L.run_all()
    by_id = {c["id"]: c for c in rep["cases"]}
    assert by_id[ok_id]["verdict"] == "pass", by_id[ok_id]
    assert by_id[bad_id]["verdict"] == "fail"
    assert by_id[skip_id]["verdict"] == "skip"
    assert (rep["passed"], rep["failed"], rep["skipped"]) == (1, 1, 1)


def test_life_suite_empty_is_ok(tmp_growth_db):
    from app.growth.suites import life

    r = life.run(budget=None, sample=1.0, out="/tmp/ge-life.json")
    assert r["status"] == "ok" and r["n"] == 0
    assert "no approved LIFE cases" in r["coverage_note"]


def test_frontier_ranks_and_matches():
    from app.growth.learn import frontier as FR

    gaps = [
        {"utterance": "book me a flight to Paris", "at": "2026-10-27T10:00:00+00:00"},
        {"utterance": "book a flight tomorrow", "at": "2026-10-27T11:00:00+00:00"},
        {"utterance": "summarize this book", "at": "2026-10-27T11:30:00+00:00"},
    ]
    rows = FR.frontier(gaps, top=5)
    assert rows[0]["count"] == 2 and isinstance(rows[0]["closest_capability"], str)


def test_review_refuses_without_tty(monkeypatch):
    import sys

    from app.growth.learn.review import review_loop

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert review_loop() == 2


def test_nightly_skipped_without_key(monkeypatch):
    import app.growth.learn.nightly as N

    monkeypatch.setattr(N, "enabled", lambda: (False, "no eval key"))
    assert N.main([]) == 0


async def test_feedback_endpoint_records_thumbs_down(tmp_growth_db, client):
    r = await client.post("/v1/growth/feedback", json={"turn_id": "t1", "value": -1})
    assert r.status_code == 200 and r.json()["failure_id"] > 0
    r = await client.post("/v1/growth/feedback", json={"turn_id": "t2", "value": 1})
    assert r.status_code == 200 and r.json()["failure_id"] is None
    r = await client.post("/v1/growth/feedback", json={"value": 0})
    assert r.status_code == 400
