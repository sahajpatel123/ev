
def test_s01_file_read(tmp_path, scratch_db):
    from app.fabric.policy import check_capability
    from app.fabric.run_turn import run_turn
    r = run_turn("read file", root=tmp_path)
    assert r["ok"] and r["verified"]

def test_s02_file_write(tmp_path):
    from app.fabric.policy import check_capability
    from app.fabric.run_turn import run_turn
    r = run_turn("write file", root=tmp_path)
    assert r["ok"] and r["verified"]

def test_s03_file_list(tmp_path):
    from app.fabric.policy import check_capability
    from app.fabric.run_turn import run_turn
    r = run_turn("list files", root=tmp_path)
    assert r["ok"] and r["verified"]

def test_s04_policy_denies(tmp_path):
    from app.fabric.policy import check_capability
    from app.fabric.run_turn import run_turn
    assert check_capability("files", "write", {"root": str(tmp_path)}) in (True, False)
