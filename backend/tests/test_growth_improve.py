"""GE6: P2 queue, brief assembly, CLI detection, apply refusals."""

from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture()
def tmp_growth_db(monkeypatch, tmp_path):
    import app.growth.store as S

    db = tmp_path / "g.sqlite"

    def _tmpconnect():
        conn = sqlite3.connect(str(db))
        conn.executescript(S.SCHEMA)
        return conn

    monkeypatch.setattr(S, "_connect", _tmpconnect)
    return db


def test_p2_seed_list_resolve(tmp_growth_db):
    from app.growth.improve import p2 as P2

    assert P2.seed() == 12 and P2.seed() == 0  # idempotent
    items = P2.open_items()
    assert len(items) == 12
    titles = [i["title"] for i in items]
    assert any("OCR" in t for t in titles) and any("AX-fallback" in t for t in titles)
    pid = items[0]["id"]
    P2.resolve(pid, "ge-improve-1")
    assert len(P2.open_items()) == 11
    assert P2.add("new item", "detail") > 0


def test_brief_assembles_all_sources(tmp_growth_db):
    from app.growth.improve import brief as B
    from app.growth.learn import queue as Q

    Q.record("ev-bad", utterance="the thing broke", prov="owner")
    text, ctx = B.build()
    assert "Open P2 items" in text and "OCR" in text
    assert "failure clusters" in text and "ev-bad" in text
    assert "Standing rules" in text and "budget" in text.lower()
    text1, ctx1 = B.build(item_id=ctx["p2"][0]["id"])
    assert "Target P2 item" in text1
    with pytest.raises(ValueError):
        B.build(item_id=9999)


def test_detect_table_and_cmds():
    from app.growth.improve import detect as D

    assert "muse" in D.TABLE and "--disable-approval" in " ".join(D.TABLE["muse"][1])
    assert D.headless_cmd_file("muse", "/tmp/p.txt") == [
        "muse", "exec", "--disable-approval", "--prompt-file", "/tmp/p.txt"]
    assert D.headless_cmd("codex", "hi")[0:2] == ["codex", "exec"]
    with pytest.raises(ValueError):
        D.headless_cmd("muse", "hi")
    with pytest.raises(ValueError):
        D.headless_cmd_file("codex", "/tmp/p.txt")
    for name in D.detect():
        assert name in D.TABLE


def test_apply_refusals(monkeypatch, tmp_path, tmp_growth_db):
    from app.growth.improve import apply as A

    monkeypatch.setattr("app.growth.improve.detect.detect", lambda: [])
    rep = A.apply(item_id=1)
    assert rep["ok"] is False and "no coding CLI" in rep["error"]
    monkeypatch.setattr("app.growth.improve.detect.detect", lambda: ["muse"])
    rep = A.apply(item_id=1, cli="nope")
    assert rep["ok"] is False and "not installed" in rep["error"]
    rep = A.apply(item_id=9999, cli="muse")
    assert rep["ok"] is False and "no P2 item" in rep["error"]
