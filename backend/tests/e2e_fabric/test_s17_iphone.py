
def test_s17_iphone_turn(tmp_path):
    from app.fabric.run_turn import run_turn
    r = run_turn("iphone status", root=tmp_path)
    assert r["ok"] and r["verified"]
