"""GE5: SLO computation, sink roundtrip, alerts, dashboard (fixtures)."""

from __future__ import annotations

import time


def _turn(i: int, **kw) -> dict:
    d = {"turn_id": f"t{i}", "ts": time.time() - 100, "modality": "typed",
         "kind": "done", "ok": True, "duration_ms": 500.0,
         "first_response_ms": 800.0, "tool_calls": 1, "spoke": True,
         "bg": False, "cost_usd": None}
    d.update(kw)
    return d


def test_compute_ok_and_breaches():
    from app.growth.slo.compute import compute

    ok_turns = [_turn(i) for i in range(12)]
    rep = compute(ok_turns, cost_day_usd=0.10)
    assert rep["slos"]["turn_success_7d"]["breached"] is False
    assert rep["slos"]["text_p95_ms"]["value"] == 800.0
    assert rep["slos"]["voice_p95_ms"]["value"] is None  # no voice data: unmeasured
    bad = [_turn(i, ok=False, spoke=False, kind="crashed") for i in range(12)]
    rep = compute(bad)
    assert rep["slos"]["turn_success_7d"]["breached"] is True
    assert rep["slos"]["silent_7d"]["value"] == 12
    small = [_turn(0, ok=False, spoke=False, kind="crashed")]
    rep = compute(small)
    assert rep["slos"]["turn_success_7d"]["breached"] is False  # n < 10 never convicts


def test_sink_roundtrip_never_stores_content(tmp_path, monkeypatch):
    from app.growth.slo import sink as S

    monkeypatch.setattr(S, "sink_path", lambda: tmp_path / "turns.jsonl")
    S.record_turn(turn_id="t-secret-utterance-should-not-land", t0=time.time() - 1,
                  modality="typed", kind="done", ok=True, spoken="hello world secret",
                  tool_calls=2)
    rows = S.read_turns()
    assert len(rows) == 1 and rows[0]["spoke"] is True
    blob = (tmp_path / "turns.jsonl").read_text()
    assert "hello world secret" not in blob and "secret-utterance" in blob


def test_alerts_check_files_breaches(monkeypatch, tmp_path):
    import app.growth.slo.alerts as A
    from app.growth.slo import sink as S

    monkeypatch.setattr(S, "sink_path", lambda: tmp_path / "turns.jsonl")
    for i in range(12):
        S.record_turn(turn_id=f"t{i}", t0=time.time() - 1, modality="typed",
                      kind="crashed", ok=False)
    async def _cost():
        return 0.05
    monkeypatch.setattr(A, "daily_cost_usd", _cost)
    monkeypatch.setattr(A, "STATE", tmp_path / "slo-state.json")
    recorded = []
    monkeypatch.setattr("app.growth.learn.queue.record",
                        lambda source, **kw: recorded.append((source, kw)) or 1)
    monkeypatch.setattr(A, "_notify", lambda text: True)
    rc = A.check()
    assert rc == 1
    assert any(s == "slo-breach" for s, _ in recorded)
    # Second check: same breach, no duplicate queue filing (deduped).
    recorded.clear()
    assert A.check() == 1
    assert recorded == []


def test_dashboard_renders(tmp_path, monkeypatch):
    import app.growth.slo.dashboard as D

    monkeypatch.setattr(D, "PAGE", tmp_path / "health.html")
    async def _nocost():
        return None
    monkeypatch.setattr("app.growth.slo.alerts.daily_cost_usd", _nocost)
    monkeypatch.setattr("app.growth.slo.sink.read_turns", lambda since_h=None: [])
    assert D.main([]) == 0
    page = (tmp_path / "health.html").read_text()
    assert "Evie Health" in page and "UNMEASURED" in page
