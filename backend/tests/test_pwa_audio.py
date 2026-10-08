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


def test_talk_uses_single_gesture_audio_context() -> None:
    """iPhone taps dying between tap_gum_ok and live/open: two in-gesture
    AudioContexts plus a live mic (capture tapCtx + playback ensure()'s 2nd
    ctx + addModule) is the top native-crash suspect. talk() must create at
    most ONE AudioContext (the capture tapCtx); playback priming stays in
    startPcm's bounded ensure(), which fails fast with the truth.
    """
    app_js = (PWA / "app.js").read_text()
    talk_at = app_js.index("async function talk()")
    talk_end = app_js.index("async function startPcm(")
    talk = app_js[talk_at:talk_end]
    assert "state._tapMic = tapMic;" in talk
    assert "tapPlayback.ensure()" not in talk
    assert "pcmEngine()" not in talk
    assert talk.count("new AudioContext") <= 1


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
    mic must be liveness-watched, the tap must carry an overall watchdog,
    teardown must never throw past the mood reset, and every tap must emit
    log-beacons so field progress is provable from the access log.
    """
    app_js = (PWA / "app.js").read_text()
    audio_js = (PWA / "audio.js").read_text()
    talk_at = app_js.index("async function talk()")
    talk_end = app_js.index("async function startPcm(")
    talk = app_js[talk_at:talk_end]
    # Bounded audio waits: resume()/addModule() fail fast, never hang.
    assert "resumeBounded" in audio_js
    assert "withTimeout" in audio_js
    assert "STARTUP_RESUME_MS" in audio_js
    assert "STARTUP_WORKLET_MS" in audio_js
    assert "audio_worklet_timeout" in audio_js
    assert "await this.ctx.resume()" not in audio_js
    # No MediaStreamSource in the tap gesture: field evidence showed taps
    # dying between the mic grant and live/open exactly when a gesture-time
    # source node was present (no network, no watchdog, no incident — a
    # native crash try/catch cannot survive). Capture attach stays post-open.
    assert "createMediaStreamSource" not in talk
    assert "connectTapSink" not in app_js
    assert "disconnectTapSink" not in app_js
    # Talk watchdog + tap-mic liveness + incident reporting.
    assert "TALK_STARTUP_MS" in app_js
    assert "talk_watchdog" in app_js
    assert "tap_mic_ended" in app_js
    assert "tap_mic_muted" in app_js
    assert "mic_muted" in app_js
    assert "armTapMicWatcher" in app_js
    assert "reportTalkIncident" in app_js
    assert "audio_worklet_timeout" in app_js
    assert "clearTimeout(state._talkWatchdog)" in app_js
    assert "state._stopError" in app_js
    # Log-beacons: the tap's trail in the access log, zero server changes.
    assert "function beacon(" in app_js
    assert "?evm=" in app_js
    assert 'beacon("tap_entry")' in app_js
    assert 'beacon("tap_gum_ok")' in app_js
    assert 'beacon("tap_gum_deny")' in app_js
    assert 'beacon("tap_ctx_ok")' in app_js
    assert 'beacon("tap_open_send")' in app_js
    assert 'beacon("tap_open_ok")' in app_js
    assert 'beacon("tap_fail")' in app_js
    assert 'beacon("pcm_ensure_ok")' in app_js
    assert 'beacon("pcm_gum_timeout")' in app_js
    assert 'beacon("pcm_ws_open")' in app_js
    assert 'beacon("pcm_listen")' in app_js
    # Discriminating instrumentation: a swallowed gUM denial must leave a
    # milestone + beacon + incident naming the rejection, or it stays
    # server-identical to a native crash.
    assert '"tap_gum_deny_" +' in talk or "'tap_gum_deny_' +" in talk
    assert 'reportTalkIncident("tap_gum_deny"' in talk
    assert talk.index('beacon("tap_gum_ok")') < talk.index('beacon("tap_ctx_ok")')
    assert talk.index('beacon("tap_ctx_ok")') < talk.index('beacon("tap_watcher_ok")')
    assert app_js.count("new AudioContext") <= 2
    assert talk.count("new AudioContext") <= 1
    # iOS "interrupted" contexts must resume like "suspended" (route flip,
    # Siri, alert, lock) instead of no-op'ing into a silent PCM stall.
    assert '"interrupted"' in audio_js
    assert '"interrupted"' in app_js
    # Fallback gUM in startPcm must be bounded (Promise.race) with a timeout
    # beacon — it was the one unbounded await left in the post-open path.
    pcm_at = app_js.index("async function startPcm(")
    pcm_end = app_js.index("function closeActiveBackend()")
    start_pcm = app_js[pcm_at:pcm_end]
    assert "Promise.race" in start_pcm
    assert "getUserMedia" in start_pcm
    assert 'beacon("pcm_gum_timeout")' in start_pcm
    assert "await navigator.mediaDevices.getUserMedia" not in start_pcm
    # Mic constraints on the tap path must match PRODUCTION_MIC_CONSTRAINTS
    # ({ ideal: 1 }, never a bare channelCount that iOS can reject).
    assert "channelCount: 1," not in talk
    assert "channelCount: 1," not in start_pcm
    assert "channelCount: { ideal: 1 }" in talk
    assert "channelCount: { ideal: 1 }" in start_pcm
    # Guarded, feature-detected play-and-record audio session before gUM.
    assert 'navigator.audioSession.type = "play-and-record"' in app_js
