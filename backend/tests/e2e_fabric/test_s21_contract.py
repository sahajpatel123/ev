
def test_s21_contract_version():
    from app.fabric.policy import contract_version
    from app.fabric.run_turn import run_turn
    assert isinstance(contract_version(), str)

def test_s21_contract_receipt(tmp_path):
    from app.fabric.policy import contract_version
    from app.fabric.run_turn import run_turn
    r = run_turn("contract check", root=tmp_path)
    assert r["ok"] and r["verified"] and r.get("contract") == contract_version()
