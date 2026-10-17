
def test_s14_builder_plan(tmp_path):
    from app.fabric.builder import build_plan, apply_plan
    p = build_plan("add feature", root=tmp_path)
    assert p["ok"] and p["verified"]

def test_s15_builder_apply(tmp_path):
    from app.fabric.builder import build_plan, apply_plan
    p = build_plan("add feature", root=tmp_path)
    r = apply_plan(p, root=tmp_path)
    assert r["ok"] and r["verified"]
