
def test_s19_grant(tmp_path):
    from app.fabric.grants import check_grant
    r = check_grant("shell", root=tmp_path)
    assert r["ok"] and r["verified"]
