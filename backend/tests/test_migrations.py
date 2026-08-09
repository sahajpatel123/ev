"""Alembic migrations: initial schema applies and rolls back cleanly."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config

from alembic import command
from app.config import settings

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _config(database_url: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def test_initial_migration_upgrades_and_downgrades(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "migration.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", url)
    cfg = _config(url)

    command.upgrade(cfg, "head")

    con = sqlite3.connect(db_path)
    tables = {
        row[0]
        for row in con.execute(
            "select name from sqlite_master where type='table'"
        ).fetchall()
    }
    version = con.execute("select version_num from alembic_version").fetchone()[0]
    con.close()

    assert version
    for expected in (
        "events",
        "memories",
        "access_log",
        "approved_actions",
        "routine_runs",
        "integrations",
        "voice_enrollments",
    ):
        assert expected in tables, f"missing table {expected}"

    command.downgrade(cfg, "base")

    con = sqlite3.connect(db_path)
    remaining = {
        row[0]
        for row in con.execute(
            "select name from sqlite_master where type='table'"
        ).fetchall()
    }
    con.close()
    assert "events" not in remaining
    assert "alembic_version" in remaining
