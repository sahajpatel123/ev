"""S27: SQL-classifier fuzz — every bypass fails closed, legit stays usable."""

from __future__ import annotations

import shutil
import sqlite3

import pytest

from app.fabric.sql_lane import classify

READ = [
    "SELECT 1",
    "SELECT a FROM t WHERE b='x;y'",
    "SELECT 'a;b'",
    'SELECT "weird;col" FROM t',
    "WITH x AS (SELECT 1) SELECT * FROM x",
    "WITH a AS (SELECT 1), b AS (SELECT 2) SELECT * FROM a, b",
    "EXPLAIN SELECT 1",
    "EXPLAIN QUERY PLAN SELECT 1",
    "VALUES (1), (2)",
    "SELECT 1 -- trailing comment",
    "/* lead */ SELECT 1",
    "SELECT 1 /* mid */ FROM t",
    "TABLE films",
    "PRAGMA table_info(notes)",
    "PRAGMA journal_mode",
    "SELECT updated FROM t",
    "select * from t; -- nothing else",
]
DML = [
    "INSERT INTO t VALUES (1)",
    "UPDATE t SET a='x' WHERE b='y;z'",
    "DELETE FROM t",
    "WITH moved AS (DELETE FROM t RETURNING *) SELECT * FROM moved",
    "WITH u AS (UPDATE t SET a=1 RETURNING *) SELECT 1",
    "INSERT INTO t SELECT * FROM u",
    "REPLACE INTO t VALUES (1)",
    "MERGE INTO t USING u ON t.id=u.id WHEN MATCHED THEN UPDATE SET a=1",
    "SELECT 1; UPDATE t SET a=1",
    "  update t set a=1  ",
]
DDL = [
    "CREATE TABLE t (a)",
    "DROP TABLE t",
    "ALTER TABLE t ADD COLUMN b",
    "TRUNCATE t",
    "CREATE INDEX i ON t(a)",
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous = OFF",
    "ATTACH 'x.db' AS x",
    "DETACH x",
    "VACUUM",
    "REINDEX",
    "SELECT 1; DROP TABLE t",
    "COPY t FROM '/etc/passwd'",
    "GRANT SELECT ON t TO u",
    "CREATE TABLE t AS SELECT ';' ",
]
DENY = [
    "SELECT load_extension('evil')",
    "SELECT readfile('/etc/passwd')",
    "SELECT writefile('/tmp/x', 'y')",
    "PRAGMA writable_schema=ON",
    "select LOAD_EXTENSION('x')",
    "COPY t FROM PROGRAM 'id'",
    "COPY (SELECT 1) TO PROGRAM 'curl evil'",
]


@pytest.mark.parametrize("sql", READ)
def test_s27_reads_stay_read(sql: str) -> None:
    assert classify(sql) == "read", sql


@pytest.mark.parametrize("sql", DML)
def test_s27_dml_detected(sql: str) -> None:
    assert classify(sql) == "dml", sql


@pytest.mark.parametrize("sql", DDL)
def test_s27_ddl_detected(sql: str) -> None:
    assert classify(sql) == "ddl", sql


@pytest.mark.parametrize("sql", DENY)
def test_s27_dangerous_denied(sql: str) -> None:
    assert classify(sql) == "deny", sql


def test_s27_execute_deny_ignores_confirm(tmp_path) -> None:
    from app.fabric.sql_lane import execute

    out = execute("SELECT load_extension('x')", db_path=":memory:",
                  root=tmp_path, confirm=True)
    assert out["ok"] is False


def test_s27_execute_pragma_bare_reads(tmp_path) -> None:
    from app.fabric.sql_lane import execute

    db = tmp_path / "p.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE t (a TEXT)")
    conn.commit()
    conn.close()
    out = execute("PRAGMA table_info(t)", db_path=str(db), root=tmp_path)
    assert out["ok"] is True


def test_s27_default_scratch_persists_across_calls(tmp_path) -> None:
    from app.fabric.sql_lane import execute

    created = execute("CREATE TABLE k (a TEXT)", root=tmp_path, confirm=True)
    assert created["ok"], created
    inserted = execute("INSERT INTO k VALUES ('v')", root=tmp_path,
                       confirm=True, preview=False)
    assert inserted["ok"], inserted
    read = execute("SELECT a FROM k", root=tmp_path)
    assert read["evidence"]["rows"] == [("v",)], read
    assert (tmp_path / ".ev-scratch.db").exists()


def test_s27_audit_redacts_secrets(tmp_path) -> None:
    from app.fabric.sql_lane import execute

    db = tmp_path / "s.db"
    execute("CREATE TABLE t (a TEXT)", db_path=str(db), root=tmp_path, confirm=True)
    execute("INSERT INTO t VALUES ('api_key=SECRET-TOKEN-123')",
            db_path=str(db), root=tmp_path, confirm=True)
    logged = (tmp_path / "sql_audit.jsonl").read_text()
    assert "SECRET-TOKEN-123" not in logged
    assert "redacted" in logged.lower()


def _docker_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        import subprocess

        p = subprocess.run(["docker", "info"], capture_output=True, timeout=15)
        return p.returncode == 0
    except Exception:
        return False


needs_docker = pytest.mark.skipif(not _docker_ok(), reason="docker daemon required")


@needs_docker
def test_s27_postgres_throwaway_roundtrip() -> None:
    from app.fabric.sql_lane import pg_execute

    name = "ev-s27-test"
    try:
        pg_execute("SELECT 1", container=name)
        pg_execute("CREATE TABLE s27t (a TEXT)", container=name, confirm=True)
        preview = pg_execute("INSERT INTO s27t VALUES ('x')", container=name,
                             preview=True)
        assert preview["ok"] and preview["evidence"]["rolled_back"] is True
        committed = pg_execute("INSERT INTO s27t VALUES ('x')", container=name,
                               confirm=True)
        assert committed["ok"]
        rows = pg_execute("SELECT a FROM s27t", container=name)
        assert rows["evidence"]["rows"] == [("x",)], rows
    finally:
        import subprocess

        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)
