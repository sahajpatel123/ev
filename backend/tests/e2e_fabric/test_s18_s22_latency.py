
def test_s18_latency(tmp_path):
    from app.fabric.run_turn import run_turn
    from app.fabric.policy import latency_budget_ms
    r = run_turn("quick op", root=tmp_path)
    assert r["ok"] and r.get("latency_ms", 0) <= latency_budget_ms()

def test_s22_latency_receipt(tmp_path):
    from app.fabric.run_turn import run_turn
    from app.fabric.policy import latency_budget_ms
    r = run_turn("quick op", root=tmp_path)
    assert "verified" in r
