"""S17: iPhone path — text/voice/screenshot reach run_turn; approval executes once."""
import pytest


@pytest.fixture(autouse=True)
def _fresh_adapter():
    from app.fabric import comms
    comms.set_adapter(comms.MemoryAdapter())
    yield
    comms.set_adapter(comms.MemoryAdapter())


def test_s17_modalities_reach_facade(tmp_path):
    from app.fabric.turn import iphone_turn
    for prompt in ("text: status", "voice: what time is it"):
        r = iphone_turn(prompt, root=tmp_path)
        assert r["ok"] and r["verified"], r
        assert r["evidence"]["prompt"] == prompt
    shot = iphone_turn("screenshot: describe", root=tmp_path,
                       ops=[{"lane": "screen", "op": "resolve_last",
                             "args": {"conversation": [{"images": ["img-1"]}]}}])
    assert shot["ok"] and shot["evidence"]["steps"][0]["evidence"]["image"] == "img-1", shot


def test_s17_approval_executes_exactly_once():
    from app.fabric import comms
    ad = comms._ad()
    parked = comms.send_message("hi mom", to="Mom", confirmed=False, key="appr-1")
    assert not parked["ok"] and ad.sent == []
    first = comms.send_message("hi mom", to="Mom", confirmed=True, key="appr-1")
    assert first["ok"], first
    replay = comms.send_message("hi mom", to="Mom", confirmed=True, key="appr-1")
    assert replay["ok"] and replay["evidence"].get("duplicate"), replay
    assert len(ad.sent) == 1, ad.sent
