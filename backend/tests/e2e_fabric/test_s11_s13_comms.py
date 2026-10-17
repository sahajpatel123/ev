
def test_s11_comms_draft():
    from app.fabric.comms import draft_message, send_message
    d = draft_message("hello there")
    assert d["ok"] and d["verified"]

def test_s13_comms_send():
    from app.fabric.comms import draft_message, send_message
    r = send_message("hello there", to="owner")
    assert r["ok"] and r["verified"]
