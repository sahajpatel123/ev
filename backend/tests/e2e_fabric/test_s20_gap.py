
def test_s20_gap(tmp_path):
    from app.fabric.gaps import report_gap
    r = report_gap("missing lane", root=tmp_path)
    assert r["ok"] and r["verified"]
