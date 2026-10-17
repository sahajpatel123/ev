
def test_s10_bridge_roundtrip(tmp_path):
    from app.fabric.bridge import Bridge
    b = Bridge(root=tmp_path)
    r = b.roundtrip({"op": "ping"})
    assert r["ok"] and r["verified"]

def test_s16_bridge_receipt(tmp_path):
    from app.fabric.bridge import Bridge
    b = Bridge(root=tmp_path)
    r = b.roundtrip({"op": "ping"})
    assert set(["ok", "verified"]) <= set(r.keys())
