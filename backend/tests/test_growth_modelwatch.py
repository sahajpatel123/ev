"""GE7: version comparison, bake-off dry-run (no network in tests)."""

from __future__ import annotations


def test_newer_than_versions():
    from app.growth.modelwatch.watch import newer_than

    seen = ["meta/muse-spark-1.3-contributor", "meta/muse-spark-1.4", "other-9.9"]
    assert newer_than("meta/muse-spark-1.3-contributor", seen, "spark") == ["meta/muse-spark-1.4"]
    assert newer_than("meta/muse-spark-9.9", seen, "spark") == []
    live = ["gemini-3.8-live-extended-thinking", "gemini-3.9-live-x"]
    assert newer_than("gemini-3.8-live-extended-thinking", live, "gemini-live") == [
        "gemini-3.9-live-x"]


def test_bakeoff_dry_run():
    from app.growth.modelwatch.bakeoff import run_bakeoff

    rep = run_bakeoff("candidate-x", dry_run=True)
    assert rep["ok"] and rep["dry_run"] and rep["candidate"] == "candidate-x"
    assert rep["pinned"]  # resolves the live pin
