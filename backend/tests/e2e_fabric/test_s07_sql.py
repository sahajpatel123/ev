"""S7: SQL lane — read AUTO, DML preview/commit/rollback, DDL snapshot, prod DENY."""
import sqlite3


def _count(db, table="notes"):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_s07_read_auto(scratch_db, tmp_path):
    from app.fabric.sql_lane import execute
    r = execute("SELECT body FROM notes", db_path=str(scratch_db), root=tmp_path)
    assert r["ok"] and r["verified"], r
    assert r["evidence"]["rows"] == [("hello",)]
    assert r["evidence"]["mode"] == "AUTO"


def test_s07_dml_preview_commit_and_deny(scratch_db, tmp_path):
    from app.fabric.sql_lane import execute
    assert _count(scratch_db) == 1
    # preview: affected rows reported, nothing committed
    p = execute("UPDATE notes SET body='x'", db_path=str(scratch_db), root=tmp_path, preview=True)
    assert p["ok"] and p["verified"], p
    assert p["evidence"]["affected_rows"] == 1
    assert p["evidence"]["rolled_back"] is True
    assert _count(scratch_db) == 1
    conn = sqlite3.connect(str(scratch_db))
    try:
        assert conn.execute("SELECT body FROM notes").fetchone()[0] == "hello"
    finally:
        conn.close()
    # deny (no confirm): parked, nothing applied
    d = execute("UPDATE notes SET body='y'", db_path=str(scratch_db), root=tmp_path)
    assert not d["ok"] and d["evidence"].get("needs_confirm"), d
    # confirm: committed
    c = execute("UPDATE notes SET body='z'", db_path=str(scratch_db), root=tmp_path, confirm=True)
    assert c["ok"] and c["verified"], c
    assert c["evidence"]["affected_rows"] == 1
    conn = sqlite3.connect(str(scratch_db))
    try:
        assert conn.execute("SELECT body FROM notes").fetchone()[0] == "z"
    finally:
        conn.close()
    # audit log exists
    assert (tmp_path / "sql_audit.jsonl").exists()


def test_s07_ddl_snapshot_and_prod_deny(scratch_db, tmp_path):
    from app.fabric.sql_lane import execute
    d = execute("CREATE TABLE t2 (id INTEGER)", db_path=str(scratch_db), root=tmp_path, confirm=True)
    assert d["ok"] and d["verified"], d
    assert d["undo"] and d["undo"].get("snapshot"), d
    # prod DB is DENIED even with confirm
    p = execute("SELECT 1", db_path=str(scratch_db), root=tmp_path, prod=True, confirm=True)
    assert not p["ok"], p


def test_s07_classify_worst_wins():
    from app.fabric.sql_lane import classify
    assert classify("SELECT 1") == "read"
    assert classify("SELECT 1; UPDATE notes SET body='a'") == "dml"
    assert classify("UPDATE notes SET body='a'; DROP TABLE notes") == "ddl"
