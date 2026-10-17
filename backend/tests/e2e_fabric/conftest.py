"""Fake lanes fixtures for e2e fabric tests."""
import sqlite3
import pytest

@pytest.fixture
def scratch_db(tmp_path):
    db = tmp_path / "scratch.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    conn.execute("INSERT INTO notes (body) VALUES ('hello')")
    conn.commit()
    conn.close()
    return db

@pytest.fixture
def fake_shell():
    class FakeShell:
        def run(self, cmd):
            return {"ok": True, "cmd": cmd, "out": "fake"}
    return FakeShell()

@pytest.fixture
def fake_screen():
    class FakeScreen:
        def capture(self):
            return {"ok": True, "pixels": b"\x00" * 8}
    return FakeScreen()
