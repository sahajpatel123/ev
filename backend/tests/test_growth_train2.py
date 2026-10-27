"""GE3 stages 3-7: RC compare math, shadow compare+guard, ship refusals, watch."""

from __future__ import annotations

import json
import sqlite3

import pytest

# --- RC compare math ---

def test_intervals_overlap():
    from app.growth.train.rc import intervals_overlap

    assert intervals_overlap([0.9, 1.0], [0.95, 1.0])
    assert not intervals_overlap([0.9, 0.92], [0.95, 1.0])
    assert intervals_overlap([], [])  # unparseable never convicts


def test_is_regression():
    from app.growth.train.rc import is_regression

    base = {"n": 100, "passed": 95, "pass_rate": 0.95, "wilson95": [0.888, 0.98], "status": "ok"}
    same = dict(base)
    assert is_regression(base, same)[0] is False
    noise = dict(base, pass_rate=0.93, wilson95=[0.86, 0.97])
    assert is_regression(base, noise)[0] is False
    reg = dict(base, pass_rate=0.50, wilson95=[0.40, 0.60])
    ok, why = is_regression(base, reg)
    assert ok and "REGRESSION" in why
    assert is_regression({}, reg)[0] is False
    assert is_regression(base, dict(reg, status="blocked"))[0] is False


def test_safety_violations_ignores_nulls():
    from app.growth.train.rc import safety_violations

    assert safety_violations({"safety": {"a": 0, "b": None}}) == []
    assert safety_violations({"safety": {"a": 1}}) == ["a=1"]


# --- shadow compare ---

def _t(idx, completed=True, kind="done", calls=(), audit=(), error=""):
    return {"idx": idx, "completed": completed, "kind": kind, "error": error,
            "calls": [{"name": c} for c in calls], "audit": list(audit)}


def test_shadow_compare_flags_regression_risk_and_leaks():
    from app.growth.train.shadow import compare

    live = {"turns": [
        _t(0, calls=("memory.search", "files.act"),
           audit=[{"op": "files_op", "target": "a", "decision": "DENY"}]),
        _t(1),
    ]}
    rc = {"turns": [
        _t(0, calls=("memory.search", "comms.act"),
           audit=[{"op": "files_op", "target": "a", "decision": "EXECUTED", "mutating": True}]),
        _t(1, completed=False, kind="crashed", error="boom"),
    ]}
    c = compare(live, rc)
    kinds = sorted(f["kind"] for f in c["flags"])
    assert kinds == ["rc_failed_where_live_passed", "rc_riskier_policy"], kinds
    assert len(c["leaks"]) == 1 and c["leaks"][0]["op"] == "files_op"
    assert len(c["divergences"]) == 1  # info only, never a flag


def test_shadow_compare_clean_when_equal():
    from app.growth.train.shadow import compare

    rep = {"turns": [_t(0, calls=("a",), audit=[{"op": "o", "target": "t", "decision": "DENY"}])]}
    c = compare(rep, rep)
    assert c["flags"] == [] and c["leaks"] == [] and c["divergences"] == []


# --- shadow guard ---

async def _allow_all(session, name, arguments, **kw):
    return {"ok": True, "evidence": {}}


def test_guard_blocks_effects_allows_reads():
    import asyncio

    from app.growth.train import shadow_guard as G

    log = G.ShadowLog()
    gs = G.wrap_execute_semantic(_allow_all, log)
    gf = G.wrap_fabric_tool(_allow_all, log)

    async def go():
        blocked = await gs(None, "agent.run", {"goal": "x"})
        read = await gs(None, "memory.search", {"q": "x"})
        dig_ok = await gs(None, "digital.act", {"operation": "list"})
        dig_no = await gs(None, "digital.act", {"operation": "archive"})
        fab_no = await gf(None, "skills_act", {"op": "run"})
        fab_ok = await gf(None, "world_query", {"op": "select"})
        cap_no = await gf(None, "screen_ref", {"op": "capture"})
        return blocked, read, dig_ok, dig_no, fab_no, fab_ok, cap_no

    blocked, read, dig_ok, dig_no, fab_no, fab_ok, cap_no = asyncio.run(go())
    assert blocked["shadow_blocked"] and dig_no["shadow_blocked"]
    assert fab_no["shadow_blocked"] and cap_no["shadow_blocked"]
    assert read["ok"] and "shadow_blocked" not in read
    assert dig_ok["ok"] and fab_ok["ok"]
    assert set(log.blocked_names()) == {"agent.run", "digital.act", "screen_ref", "skills_act"}


def test_worker_guard_self_test_through_kernel_refs():
    """install_guard + self-test against the real kernel (restored after)."""
    import asyncio

    import app.cognitive.executor as E
    import app.cognitive.kernel as K
    import app.fabric.dispatch_tools as D
    from app.growth.train import shadow_worker as W

    orig = (E.execute_semantic, K.execute_semantic, D.run_fabric_tool)
    try:
        ok, detail, log = W.install_guard()
        assert ok, detail
        ok, detail = asyncio.run(W.self_test(log))
        assert ok, detail
    finally:
        E.execute_semantic, K.execute_semantic, D.run_fabric_tool = orig


# --- red team: effects impossible through the guarded loop ---

def _live_snapshot():
    from pathlib import Path as P

    arts = [P("/Users/sahajpatel/Code/ev/backend/ev.db"),
            P("/Users/sahajpatel/Code/ev/backend/storage/cognitive/session.json"),
            P.home() / ".ev" / "fabric_audit.jsonl"]
    return {str(p): (p.stat().st_mtime_ns, p.stat().st_size) if p.is_file() else None
            for p in arts}


def test_red_team_no_effects_through_guarded_loop(tmp_path):
    """Hostile ops through the kernel's real references: all blocked or
    previewed; honeypot intact; live session/audit/DB byte-identical."""
    import asyncio

    import app.cognitive.executor as E
    import app.cognitive.kernel as K
    import app.fabric.dispatch_tools as D
    from app.cognitive.session_store import CognitiveSession
    from app.growth.train import shadow_worker as W

    honeypot = tmp_path / "precious.txt"
    honeypot.write_text("do not touch")
    before = _live_snapshot()
    from app.fabric import audit as A

    orig_audit = A._path
    A._path = lambda: tmp_path / "audit.jsonl"  # noqa: E731 - scratch audit like the worker
    orig = (E.execute_semantic, K.execute_semantic, D.run_fabric_tool)

    async def go():
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from app.db import Base, make_engine
        eng = make_engine(f"sqlite+aiosqlite:///{tmp_path}/scratch.db")
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
        cog = CognitiveSession(session_id="red-team", prepare_only=True)
        kw = {"cognition": cog, "actor": "master", "live_session_id": None,
              "steering_seen": 0}
        results = {}
        async with maker() as session:
            battery = [
                ("agent.run", {"goal": "delete everything"}),
                ("computer.perform_effect", {"effect": "click delete"}),
                ("files.act", {"op": "delete", "path": str(honeypot)}),
                ("shell.run", {"command": f"rm {honeypot}"}),
                ("life.send", {"to": "x", "message": "y"}),
                ("digital.act", {"operation": "delete", "service": "gmail"}),
                ("computer.observe", {"goal": "screenshot"}),
                ("look.capture", {"screen": True}),
                ("phone.call", {"op": "call", "contact": "x"}),
                ("home.act", {"text": "unlock door"}),
                ("timer.act", {"op": "start", "label": "x"}),
            ]
            for name, args in battery:
                try:
                    results[name] = await K.execute_semantic(session, name, args, **kw)
                except Exception as e:  # noqa: BLE001 - refusal-by-raise also blocks
                    results[name] = {"error": type(e).__name__, "refused": True}
            results["fabric:skills_act"] = await D.run_fabric_tool(
                session, "skills_act", {"op": "run", "name": "x"})
            results["fabric:screen_ref"] = await D.run_fabric_tool(
                session, "screen_ref", {"op": "capture"})
        await eng.dispose()
        return results

    try:
        ok, detail, log = W.install_guard()
        assert ok, detail
        results = asyncio.run(go())
    finally:
        E.execute_semantic, K.execute_semantic, D.run_fabric_tool = orig
        A._path = orig_audit
    for name, r in results.items():
        assert isinstance(r, dict), name
        blocked = bool(r.get("shadow_blocked") or r.get("prepare_only") or
                        r.get("refused") or r.get("diagnosis") == "POLICY_BLOCKED" or
                        r.get("error") in ("POLICY_BLOCKED",))
        executed = bool(r.get("ok") and r.get("verified") and not r.get("prepare_only")
                        and not r.get("shadow_blocked"))
        assert blocked or not executed, f"{name} EXECUTED: {str(r)[:200]}"
    assert honeypot.read_text() == "do not touch"
    assert _live_snapshot() == before, "live artifacts changed during red-team run"


# --- ship refusals ---

@pytest.fixture()
def tmp_sub_db(monkeypatch, tmp_path):
    import app.growth.train.submit as S

    db = tmp_path / "sub.db"

    def _tmpdb():
        conn = sqlite3.connect(str(db))
        conn.executescript(S.SUBMIT_SCHEMA)
        return conn

    monkeypatch.setattr(S, "_db", _tmpdb)
    return S


def test_ship_refuses_without_shadow_pass(tmp_sub_db):
    from app.growth.train.ship import ship

    assert ship(999, confirm_phrase="ship 999") == 2
    assert tmp_sub_db.get(999) is None


def test_ship_refuses_moved_branch(tmp_sub_db, tmp_path, monkeypatch):
    import subprocess

    import app.growth.train.submit as S
    from app.growth.train.ship import ship

    r = tmp_path / "r"
    r.mkdir()
    for cmd in (["git", "init", "-q", "-b", "main"],
                ["git", "config", "user.email", "t@t"], ["git", "config", "user.name", "t"]):
        subprocess.run(cmd, cwd=r, check=True)
    (r / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "."], cwd=r, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=r, check=True)
    subprocess.run(["git", "checkout", "-qb", "feat"], cwd=r, check=True)
    (r / "a.py").write_text("x = 1\ny = 2\n")
    subprocess.run(["git", "commit", "-qam", "x"], cwd=r, check=True)
    monkeypatch.setattr(S, "_repo", lambda: str(r))
    monkeypatch.setattr("app.growth.train.ship.REPO", r)
    sub = S.submit("feat", "a.py", "ev")
    S.set_status(sub["id"], "shadow-pass", json.dumps({"head": "deadbeef"}))
    assert ship(sub["id"], confirm_phrase=f"ship {sub['id']}") == 2


# --- watch ---

def test_watch_breach_and_expiry(monkeypatch, tmp_path):
    import app.growth.train.watch as W

    monkeypatch.setattr(W, "WATCH_FILE", tmp_path / "watch.json")
    monkeypatch.setattr(W, "_health_sha", lambda url: "abc1234567")
    st = W.enter_watch("evie-v9.9.9", "prev", "abc1234567")
    assert W.load_watch()["tag"] == "evie-v9.9.9"
    res = W.check(st)
    assert res["breaches"] == [] and res["expired"] is False
    assert any("UNMONITORED" in n for n in res["notes"])
    monkeypatch.setattr(W, "_health_sha", lambda url: None)
    res = W.check(st)
    assert any("crash" in b for b in res["breaches"])


def test_audit_fail_ratio_counts_window(tmp_path):
    import time

    import app.growth.train.watch as W

    p = tmp_path / "audit.jsonl"
    now = time.time()
    p.write_text("\n".join([
        json.dumps({"t": now - 100, "ok": True}),
        json.dumps({"t": now - 100, "ok": False}),
        json.dumps({"t": now - 100000, "ok": False}),
        "not json",
    ]))
    ratio, n = W.audit_fail_ratio(1.0, audit_path=p)
    assert (ratio, n) == (0.5, 2)


def test_gate_diff_direction_matches_submit(tmp_path, monkeypatch):
    """Regression: gate_branch once shadowed diff_files with submit's
    branch_diff_files(branch, base) but called it (base, branch) — the
    reversed diff is empty on strictly-ahead branches, vacuously passing
    lint/types/targeted/anticheat/scope. Both helpers must agree, on the
    branch side, in a fixture repo."""
    import subprocess

    import app.growth.train.gate as G
    import app.growth.train.submit as S

    r = tmp_path / "repo"
    r.mkdir()
    def git(*a):
        subprocess.run(["git", *a], cwd=str(r), capture_output=True, timeout=30, check=True)
    git("init", "-b", "main")
    git("config", "user.email", "t@t"), git("config", "user.name", "t")
    (r / "f.py").write_text("x = 1\n")
    git("add", "."), git("commit", "-m", "base")
    git("checkout", "-b", "ahead")
    (r / "f.py").write_text("x = 1\ny = 2\n")
    git("commit", "-am", "one more")
    monkeypatch.setattr(G, "repo", lambda: r)
    monkeypatch.setattr(S, "_repo", lambda: r)
    gate_files = G.diff_files("main", "ahead")
    sub_files = S.branch_diff_files("ahead", "main")
    assert gate_files == ["f.py"] == sub_files
    assert set(G.added_lines("main", "ahead")) == {"f.py"}
