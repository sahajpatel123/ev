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
    assert 'AUDIO_ENGINE_VERSION = "4"' in audio_js
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


def test_talk_primes_playback_engine_inside_tap_gesture() -> None:
    """iPhone 1-2s auto-close: startPcm runs after the live/open await, so a
    playback AudioContext created there starts suspended on iOS and its
    resume() promise never resolves — every tap self-teardowns with the mic
    briefly live (live/open 200, no WS connect). talk() must prime the
    playback engine in the tap gesture, right after the capture priming and
    strictly before the first network await.
    """
    app_js = (PWA / "app.js").read_text()
    talk_at = app_js.index("async function talk()")
    talk_end = app_js.index("async function startPcm(")
    talk = app_js[talk_at:talk_end]
    assert "state._tapMic = tapMic;" in talk
    assert "tapPlayback.ensure()" in talk
    assert talk.index("state._tapMic = tapMic;") < talk.index("tapPlayback.ensure()")
    assert talk.index("tapPlayback.ensure()") < talk.index("/v1/device-gateway/live/open")


def test_talk_status_and_resume_never_block_gesture() -> None:
    """The 2s silent auto-close: talk() stalled pre-open with zero UI change
    (frozen "Ready", mic dot on then off). Status must change synchronously
    in the tap before any await, and the gesture AudioContext resume must
    never be awaited (iOS can stall it even in-gesture); attachCapture
    re-resumes with a timeout and reports the truth instead.
    """
    app_js = (PWA / "app.js").read_text()
    talk_at = app_js.index("async function talk()")
    talk_end = app_js.index("async function startPcm(")
    talk = app_js[talk_at:talk_end]
    assert 'setMood("Connecting microphone…");' in talk
    assert talk.index('setMood("Connecting microphone…");') < talk.index(
        "await navigator.mediaDevices.getUserMedia"
    )
    assert "await tapCtx.resume()" not in talk
    assert "void tapCtx.resume()" in talk
    assert "markTalkMilestone" in talk
    assert "talk_milestones" in app_js


def test_audio_startup_awaits_are_bounded() -> None:
    out = subprocess.check_output(["node", str(PWA / "tests" / "audio_startup_test.js")], text=True)
    assert "audio_startup_ok" in out


def test_talk_startup_defense_in_depth() -> None:
    """iPhone stuck-on-"Connecting microphone…" with the mic dot dying 1-2s
    in: every startup await behind the mic button must be bounded, the tap
    mic must be liveness-watched and consumed in-gesture, the tap must carry
    an overall watchdog, and teardown must never throw past the mood reset.
    """
    app_js = (PWA / "app.js").read_text()
    audio_js = (PWA / "audio.js").read_text()
    # Bounded audio waits: resume()/addModule() fail fast, never hang.
    assert "resumeBounded" in audio_js
    assert "withTimeout" in audio_js
    assert "STARTUP_RESUME_MS" in audio_js
    assert "STARTUP_WORKLET_MS" in audio_js
    assert "audio_worklet_timeout" in audio_js
    assert "await this.ctx.resume()" not in audio_js
    # Talk watchdog + tap-mic liveness + gesture sink + incident reporting.
    assert "TALK_STARTUP_MS" in app_js
    assert "talk_watchdog" in app_js
    assert "tap_mic_ended" in app_js
    assert "armTapMicWatcher" in app_js
    assert "connectTapSink" in app_js
    assert "reportTalkIncident" in app_js
    assert "audio_worklet_timeout" in app_js
    assert "clearTimeout(state._talkWatchdog)" in app_js
    assert "state._stopError" in app_js
    assert app_js.count("new AudioContext") <= 2
