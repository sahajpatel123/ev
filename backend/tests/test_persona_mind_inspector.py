"""Inspector: state snapshot, token gate, lab isolation, read-only scope."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest


@pytest.fixture()
def mind_db(tmp_path, monkeypatch):
    db = tmp_path / "mind.sqlite"
    monkeypatch.setenv("EV_PERSONA_MIND_DB", str(db))
    monkeypatch.setattr("app.persona_mind.settings.MIND_ENABLED", True)
    monkeypatch.setenv("EV_PERSONA_MIND_NO_WORKER", "1")
    from app.persona_mind import fast, store

    store.reset_connections()
    fast.invalidate()
    yield db
    store.reset_connections()
    fast.invalidate()


def _seed():
    from datetime import UTC

    from app.persona_mind import observe
    from app.persona_mind.types import TurnRecord
    from app.persona_mind.worker import drain_once

    old = datetime.now(UTC) - timedelta(seconds=60)
    for i in range(3):
        observe(TurnRecord(
            owner_id="owner", thread_id="t", turn_id=f"n{i}", ts=old,
            owner_msg=f"message {i}", evie_reply=f"reply {i}",
            channel="text", audience="private", tone={},
            reaction_to_prev="neutral", receipts=(), now_line=None,
        ))
    drain_once()


def test_build_state_keys(mind_db):
    from app.persona_mind.inspector import build_state

    _seed()
    state = build_state("owner")
    for key in ("owner", "data", "room", "mind_block", "lessons",
                "reflections", "rejections", "dreams", "latency"):
        assert key in state
    assert state["data"]["affect"]
    assert state["room"]["pace"] in ("blitz", "normal", "deep")
    assert state["mind_block"] and state["mind_block"].startswith("MIND")
    assert "snapshot_p99_ms" in state["latency"]


def test_token_gate(mind_db):
    from fastapi.testclient import TestClient

    from app.persona_mind.inspector import create_app

    client = TestClient(create_app("secret-token", "owner"), raise_server_exceptions=False)
    assert client.get("/t/wrong-token/").status_code == 404
    assert client.get("/api/state").status_code == 404
    page = client.get("/t/secret-token/")
    assert page.status_code == 200
    assert "Mind Inspector" in page.text
    assert "http" not in page.text.replace("http://127.0.0.1", "").replace("https", "") \
        or "fonts" not in page.text  # no external requests
    data = client.get("/t/secret-token/api/state").json()
    assert data["owner"] == "owner"


def test_lab_namespace_isolated(mind_db, tmp_path, monkeypatch):
    from app.persona_mind import store
    from app.persona_mind.inspector import _lab_db_path, lab_chat

    assert lab_chat("")["error"] == "empty message"
    monkeypatch.setattr(
        "app.persona_mind.reflect.brain_available", lambda: False)
    result = lab_chat("hello lab")
    assert "brain unavailable" in result["error"]
    assert store.queued_count() == 0  # real DB untouched
    assert _lab_db_path().name == "mind-lab.sqlite"
    assert not _lab_db_path().exists()  # nothing created without a brain


def test_read_only_scope(mind_db):
    import sqlite3

    from app.persona_mind import store
    from app.persona_mind.inspector import build_state
    from app.persona_mind.paths import db_scope, read_only_uri

    _seed()
    uri = read_only_uri(str(mind_db))
    with db_scope(uri):
        state = build_state("owner")
        assert state["data"]["affect"]
        with pytest.raises(sqlite3.OperationalError):
            store.log_rejection("k", "i", "r")
    assert store.rejection_stats() == {}
