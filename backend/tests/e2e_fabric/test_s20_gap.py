"""S20: unknown request -> gap record + honest answer + closest alternative."""
import json


def test_s20_gap_record_and_alternative(tmp_path):
    from app.fabric.gaps import closest_alternative, record_gap
    r = record_gap("teleport the cat", root=tmp_path)
    assert r["ok"] and r["verified"], r
    assert r["evidence"]["gap"] == "teleport the cat"
    assert r["evidence"]["alternative"], r
    assert closest_alternative("teleport the cat")
    log = tmp_path / ".ev-gaps.jsonl"
    assert log.exists()
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert any("teleport the cat" in row.get("gap", "") for row in rows)


def test_s20_unknown_lane_never_phantom(tmp_path):
    from app.fabric.run_turn import run_turn
    r = run_turn("do magic", root=tmp_path,
                 ops=[{"lane": "teleport", "op": "now", "args": {}}])
    # turn reports failure honestly; the step is a gap record with an alternative
    assert not r["ok"], r
    step = r["evidence"]["steps"][0]
    assert step["ok"] and step["evidence"].get("alternative"), step


async def test_s20_unknown_tool_records_central_gap(
    tmp_path, monkeypatch: pytest.MonkeyPatch, db_session
) -> None:
    """Gap learning wired to the live kernel: unknown tools land centrally."""
    from app.cognitive.executor import execute_semantic
    from app.cognitive.session_store import CognitiveSession, reset_for_tests

    gaps = tmp_path / "gaps.jsonl"
    monkeypatch.setattr("app.fabric.gaps.central_path", lambda: gaps)
    reset_for_tests()
    try:
        out = await execute_semantic(
            db_session, "nope.missing", {"x": 1},
            cognition=CognitiveSession(session_id="gap"),
            actor="master", live_session_id=None, steering_seen=0)
    finally:
        reset_for_tests()
    assert out["error"] == "CAPABILITY_UNAVAILABLE"
    assert gaps.is_file() and "tool:nope.missing" in gaps.read_text()
