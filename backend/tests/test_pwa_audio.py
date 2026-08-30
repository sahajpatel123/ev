"""Exactly-once PWA playback + sandbox live fencing."""

from __future__ import annotations

import subprocess
from pathlib import Path

from app.device_gateway.live_fence import fence_sandbox_lives
from app.voice.live.events import ConversationMovedEvent
from app.voice.live.layer import register_live, reset_live_registry

ROOT = Path(__file__).resolve().parents[1]
PWA = ROOT / "clients" / "pwa"


class _FakeLive:
    def __init__(self, *, session_id: str, device_id: str, memory_scope: str) -> None:
        self.session_id = session_id
        self.device_id = device_id
        self.memory_scope = memory_scope
        self.closed = False
        self._closed = False
        self.events: list[object] = []

    def now(self) -> int:
        return 1

    async def emit(self, event: object) -> None:
        self.events.append(event)

    def close(self) -> None:
        self.closed = True
        self._closed = True


def test_pcm_scheduler_never_overlaps_chunks() -> None:
    out = subprocess.check_output(["node", str(PWA / "audio_scheduler_test.js")], text=True)
    assert "audio_scheduler_ok" in out


def test_greeting_duplication_invariants() -> None:
    app_js = (PWA / "app.js").read_text()
    audio_js = (PWA / "audio.js").read_text()
    assert "EvieAudioPlaybackEngine" in app_js
    assert "playPcm16" not in app_js
    assert "node.start();" not in app_js
    assert "node.start(plan.start)" in audio_js
    assert "LinearResampler" in audio_js
    assert "worklet-ring-buffer" in audio_js
    assert "AUDIO_ENGINE_VERSION = \"3\"" in audio_js
    assert "mute.gain.value = 0" in app_js
    assert 'msg.type === "tts_chunk"' in app_js
    assert "type: \"playback\"" in app_js
    assert "BroadcastChannel" in app_js
    assert "audio_owner_lost" in app_js
    assert app_js.count("new AudioContext") <= 2


async def test_sandbox_fence_leaves_owner_mac_live() -> None:
    reset_live_registry()
    owner = _FakeLive(session_id="mac", device_id="mac-1", memory_scope="owner")
    phone_a = _FakeLive(session_id="a", device_id="p1", memory_scope="sandbox")
    phone_b = _FakeLive(session_id="b", device_id="p2", memory_scope="sandbox")
    register_live(owner)
    register_live(phone_a)
    register_live(phone_b)
    closed = await fence_sandbox_lives(except_live=phone_b)
    assert closed == 1
    assert phone_a.closed is True
    assert phone_b.closed is False
    assert owner.closed is False
    assert any(isinstance(ev, ConversationMovedEvent) for ev in phone_a.events)
    reset_live_registry()


def test_conversation_moved_event_is_typed() -> None:
    event = ConversationMovedEvent(at_ms=9, to_device_id="dev", reason="lease")
    payload = event.as_dict()
    assert payload["type"] == "conversation_moved"
    assert payload["code"] == "audio_owner_lost"
