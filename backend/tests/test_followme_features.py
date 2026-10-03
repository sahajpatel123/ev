"""Follow-Me feature service tests: clipboard, pocket executive, look,
remote, presence HUD, wake mirror."""

from __future__ import annotations

import pytest

from app.everywhere import followme_features as ff
from app.everywhere.followme import bus


@pytest.fixture(autouse=True)
def _clean():
    bus.reset()
    ff.clipboard_clear()
    yield
    bus.reset()
    ff.clipboard_clear()


def _cand(device_id, presence="ONLINE", caps=(), device_type="phone", age_s=10):
    from datetime import UTC, datetime, timedelta

    return {
        "device_id": device_id,
        "display_name": device_id,
        "device_type": device_type,
        "presence_state": presence,
        "capabilities": list(caps),
        "last_seen_at": (datetime.now(UTC) - timedelta(seconds=age_s)).isoformat(),
        "trust_state": "TRUSTED_OWNER_DEVICE",
    }


def test_clipboard_push_lists_and_expires():
    ff.clipboard_push(source_device_id="mac", kind="text", text="hello", ttl_seconds=1800)
    assert ff.clipboard_list()[0]["text"] == "hello"
    item2 = ff.clipboard_push(source_device_id="se", kind="url", text="https://x", ttl_seconds=1)
    item2["expires_at"] = "2020-01-01T00:00:00+00:00"
    # expire manually
    ff._clipboard[1] = item2
    texts = [i["text"] for i in ff.clipboard_list()]
    assert "https://x" not in texts


def test_clipboard_validation():
    with pytest.raises(ValueError):
        ff.clipboard_push(source_device_id="mac", kind="blob", text="x", ttl_seconds=60)
    with pytest.raises(ValueError):
        ff.clipboard_push(source_device_id="mac", kind="text", text="   ", ttl_seconds=60)


def test_heading_out_posts_intents():
    cands = [_cand("16pro", caps=["media"]), _cand("mac", device_type="laptop")]
    out = ff.heading_out(source_device_id="mac", candidates=cands)
    assert out["bundle"] == ff.HEADING_OUT_BUNDLE
    assert len(out["intents"]) == 2
    assert out["primary_device_id"] == "16pro"
    kinds = [i["kind"] for i in bus.list()]
    assert "focus.handoff" in kinds and "media.duck" in kinds


def test_request_look_targets_camera_device():
    cands = [_cand("mac", caps=["camera"], device_type="laptop"), _cand("se", caps=[])]
    out = ff.request_look(source_device_id="16pro", reason="what is this", kind="photo", candidates=cands)
    assert out["target_device_id"] == "mac"
    assert out["intent"]["kind"] == "look.request"


def test_request_look_validation():
    with pytest.raises(ValueError):
        ff.request_look(source_device_id="x", reason="r", kind="hologram", candidates=[])


def test_remote_request_allowlist_and_targeting():
    cands = [_cand("16pro", caps=["device.echo"])]
    out = ff.request_remote(source_device_id="mac", action="device.echo", device_id="16pro", candidates=cands)
    assert out["request_id"]
    with pytest.raises(ValueError):
        ff.request_remote(source_device_id="mac", action="self.destruct", device_id=None, candidates=cands)
    with pytest.raises(KeyError):
        ff.request_remote(source_device_id="mac", action="device.echo", device_id="ghost", candidates=cands)


def test_presence_hud_shape():
    cands = [_cand("16pro"), _cand("mac", device_type="laptop")]
    ff.heading_out(source_device_id="mac", candidates=cands)
    hud = ff.presence_hud(cands)
    assert hud["primary_device_id"] == "16pro"
    assert len(hud["devices"]) == 2
    kinds = {d["device_id"]: d["recent_intent_kinds"] for d in hud["devices"]}
    assert "focus.handoff" in kinds["16pro"]


def test_wake_mirror_deterministic_id():
    out = ff.wake_mirror(source_device_id="mac", thread_id="t123", device_label="MacBook")
    assert out["mirror_thread_id"] == "t123:mirror"
    with pytest.raises(ValueError):
        ff.wake_mirror(source_device_id="mac", thread_id="  ", device_label="x")


def test_presence_hud_dedupes_same_display_name():
    # A paired registration and an auto-created heartbeat shadow can share
    # a display name; the HUD must show one card, preferring strongest
    # presence, and must not hide receipts recorded on either row.
    online = _cand("cead", presence="ONLINE", device_type="laptop")
    shadow = _cand("shadow", presence="OFFLINE", device_type="laptop")
    online["display_name"] = "Mac"
    shadow["display_name"] = "Mac"
    bus.post(
        kind="focus.handoff",
        capability=None,
        args={},
        source_device_id="16pro",
        target={"device_id": "shadow", "display_name": "Mac", "presence_state": "OFFLINE"},
    )
    hud = ff.presence_hud([shadow, online])
    assert len(hud["devices"]) == 1
    kept = hud["devices"][0]
    assert kept["device_id"] == "cead"
    assert kept["presence_state"] == "ONLINE"
    assert "focus.handoff" in kept["recent_intent_kinds"]
