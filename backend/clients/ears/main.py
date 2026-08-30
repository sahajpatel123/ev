"""Always-on ears process: mic → ring → VAD → wake → scene → Agent 4."""

from __future__ import annotations

import argparse
import array
import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.audio.capture import (
    MicrophoneDeniedError,
    MicrophoneStream,
    MicrophoneUnavailableError,
    list_input_devices,
    pcm_to_wav_bytes,
)
from app.audio.ring import PCM16RingBuffer, pcm16_bytes
from app.audio.scene import classify_wav, default_scene_classifier, set_scene_classifier
from app.audio.vad import StreamingSegmenter, default_vad_engine, looks_stuck_loop
from app.config import settings
from clients.ears.live import EarsLiveChannel, EarsLivePlayer, EarsLiveUnavailable
from clients.ears.wake import PhraseFallbackWake

#: Voice session states in which the ears process may keep streaming follow-up
#: utterances without re-waking. Mirrors ``ACTIVE_STATES`` in the lifecycle.
ACTIVE_SESSION_STATES = frozenset(
    {"awake", "follow_up", "processing", "responding", "verifying"}
)

LOGGER = logging.getLogger("ears")

#: Marker written by EV.app for the whole duration of a Realtime session.
#: ONE mic owner law: while the marker names a live process, ears must not
#: hold the input device — the accepted-wake handoff owns it. PID-liveness
#: means a crashed app can never wedge ears out of the microphone.
EV_LIVE_MIC_MARKER = Path.home() / "Library" / "Application Support" / "EV" / "live-mic-owner"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def ev_live_owns_mic() -> bool:
    """True while EV.app's live session owns the microphone.

    The marker carries the owning app's PID; a stale marker whose PID is
    gone is ignored so a crashed EV.app can never permanently silence the
    always-on listener.
    """
    try:
        raw = EV_LIVE_MIC_MARKER.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    try:
        return _pid_alive(int(raw))
    except ValueError:
        return False


async def wait_for_mic_ownership(poll_s: float = 5.0, max_wait_s: float = 3600.0) -> bool:
    """Wait until the live session releases the mic (marker gone/owner dead).

    Returns True when ears may open the input. Bounded so a pathological
    marker cannot hang the service forever; launchd KeepAlive restarts the
    process if the wait gives up.
    """
    waited = 0.0
    logged = 0.0
    while ev_live_owns_mic():
        if waited >= max_wait_s:
            LOGGER.warning("ears: live-mic marker waited %.0fs — giving up this cycle", waited)
            return False
        if waited - logged >= 30.0:
            LOGGER.info("ears: EV.app live session owns the mic; standing down (%.0fs)", waited)
            logged = waited
        await asyncio.sleep(poll_s)
        waited += poll_s
    return True


def menu_bar_app_running() -> bool:
    """True when the EV menu-bar app process is alive.

    The always-on wake listener must never run without the menu-bar app: the
    microphone stays active only while the user has EV open. The EV.app
    process is detected by its bundle path so both a clean quit and a crash
    (which never runs the app's quit handler) release the microphone.
    """
    result = subprocess.run(
        ["pgrep", "-f", "EV.app/Contents/MacOS/EV"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def start_app_watchdog(
    app_alive: Callable[[], bool],
    *,
    interval_s: float = 5.0,
    on_exit: Callable[[], None] | None = None,
) -> threading.Event:
    """Force-exit this process when the EV menu-bar app disappears.

    The main asyncio loop can be blocked in a long HTTP/TTS call and would not
    notice the app quitting for a full timeout. A separate daemon thread checks
    periodically and hard-exits with ``os._exit`` — process death makes the OS
    reclaim the microphone instantly, which is exactly what the user expects
    when they quit EV. KeepAlive=false on the launchd job means it stays dead.

    The returned ``Event`` stops the watchdog thread; ``run_ears`` sets it on
    shutdown so the thread never outlives the loop (which would hard-exit the
    host process, including pytest, from the next test's timeline). ``on_exit``
    is injectable so tests can observe the shutdown decision without dying.
    """

    exit_fn = on_exit or (lambda: os._exit(0))
    stop = threading.Event()

    def watch() -> None:
        while not stop.is_set():
            stop.wait(interval_s)
            if stop.is_set():
                return
            try:
                alive = app_alive()
            except Exception:  # noqa: BLE001 - a flaky check must not kill ears
                alive = True
            if not alive:
                LOGGER.warning(
                    "ears: EV menu-bar app is not running; exiting (mic released)"
                )
                exit_fn()

    threading.Thread(target=watch, name="ears-app-watchdog", daemon=True).start()
    return stop


@dataclass
class EarConfig:
    device: str | None = None
    sample_rate: int = 16000
    ring_seconds: float = 10.0  # 5-10s rolling ring, never uploaded merely because it exists
    block_ms: int = 20
    device_id: str = "mac-ears"
    vad_model_path: str | None = None
    vad_threshold: float = 0.5
    vad_pre_roll_s: float = 1.0  # WAKE W1: ~1-2s useful pre-roll from real wake timing (was 0.4)
    vad_post_roll_s: float = 0.75
    vad_min_speech_s: float = 0.2
    max_segment_s: float = 60.0
    listen_max_segment_s: float = 20.0
    http_timeout_s: float = 45.0
    echo_tail_s: float = 0.6
    # WAKE W1: 1.5s wake_chunk + 1.0s pre-roll → ~1-2s handoff (ring 10s; mic stable, never restarted per wake)
    wake_chunk_s: float = 1.5
    idle_min_rms: float = 140.0
    idle_min_peak: int = 600
    wake_model_path: str | None = None
    wake_verifier_path: str | None = None
    wake_threshold: float = 0.5
    wake_local_spotter: bool = True
    wake_strict_name: bool = True
    wake_asr_model: str = "tiny"
    stuck_loop_drop: bool = True
    stuck_loop_threshold: float = 0.10
    stream_playback: bool = True
    live_enabled: bool = True
    scene_model_path: str | None = None
    scene_labels_path: str | None = None
    api_url: str | None = None
    api_key: str | None = None
    consent: bool = False
    dry_run: bool = False
    save_segments_dir: str | None = None
    report_interval_s: float = 300.0
    duration_s: float | None = None
    log_level: str = "INFO"
    simulate_wav: str | None = None
    resource_report: str | None = None

    @property
    def pre_roll_samples(self) -> int:
        return int(self.vad_pre_roll_s * self.sample_rate)


@dataclass
class EarRunStats:
    blocks: int = 0
    segments: int = 0
    wake_hits: int = 0
    utterances_sent: int = 0
    scenes: dict = field(default_factory=dict)
    started_at: float = field(default_factory=time.monotonic)


def pcm_peak_rms(samples) -> tuple[int, float]:
    """Peak absolute sample and RMS for a PCM16 block."""

    if not samples:
        return 0, 0.0
    peak = 0
    acc = 0
    for sample in samples:
        value = abs(int(sample))
        if value > peak:
            peak = value
        acc += int(sample) * int(sample)
    return peak, (acc / len(samples)) ** 0.5


def idle_clip_worth_spotting(
    samples,
    *,
    min_rms: float,
    min_peak: int,
) -> bool:
    """True when a clip is loud enough to be speech, not room tone."""

    peak, rms = pcm_peak_rms(samples)
    return rms >= min_rms or peak >= min_peak


def build_config(args: argparse.Namespace | None = None) -> EarConfig:
    """Config from settings + CLI flags (CLI wins)."""

    api_url = settings.ears_api_url
    api_key = settings.ears_api_key
    host = (urlparse(api_url).hostname or "").lower() if api_url else ""
    if not api_key and host in {"localhost", "127.0.0.1", "::1"}:
        # Keep zero-setup local development working without ever sending the
        # root key to a non-loopback API URL.
        api_key = settings.master_key
    elif api_url and not api_key:
        LOGGER.error(
            "EV_EARS_API_KEY is required for non-loopback ears API delivery; "
            "refusing to use EV_MASTER_KEY as a remote bearer token"
        )

    cfg = EarConfig(
        device=settings.ears_device,
        sample_rate=settings.ears_sample_rate,
        ring_seconds=settings.ears_ring_seconds,
        block_ms=settings.ears_block_ms,
        device_id=settings.ears_device_id,
        vad_model_path=settings.ears_vad_model_path,
        vad_threshold=settings.ears_vad_threshold,
        vad_pre_roll_s=settings.ears_vad_pre_roll_s,
        vad_post_roll_s=settings.ears_vad_post_roll_s,
        vad_min_speech_s=settings.ears_vad_min_speech_s,
        max_segment_s=settings.ears_max_segment_s,
        listen_max_segment_s=settings.ears_listen_max_segment_s,
        http_timeout_s=settings.ears_http_timeout_s,
        wake_chunk_s=settings.ears_wake_chunk_s,
        idle_min_rms=settings.ears_idle_min_rms,
        idle_min_peak=settings.ears_idle_min_peak,
        wake_model_path=settings.voice_wake_openwakeword_model_path,
        wake_verifier_path=settings.voice_wake_openwakeword_verifier_path,
        wake_threshold=settings.ears_wake_threshold,
        wake_local_spotter=settings.ears_wake_local_spotter,
        wake_strict_name=settings.ears_wake_strict_name,
        wake_asr_model=settings.ears_wake_asr_model,
        stuck_loop_drop=settings.ears_stuck_loop_drop,
        stuck_loop_threshold=settings.ears_stuck_loop_threshold,
        stream_playback=settings.ears_stream_playback,
        live_enabled=settings.ears_live_enabled,
        scene_model_path=settings.ears_scene_model_path,
        scene_labels_path=settings.ears_scene_labels_path,
        api_url=api_url,
        api_key=api_key,
        consent=settings.ears_consent,
        dry_run=settings.ears_dry_run,
        save_segments_dir=settings.ears_save_segments_dir,
        report_interval_s=settings.ears_report_interval_s,
    )
    if args is None:
        return cfg
    overrides = {
        "device": args.device,
        "sample_rate": args.sample_rate,
        "ring_seconds": args.ring_seconds,
        "block_ms": args.block_ms,
        "device_id": args.device_id,
        "vad_model_path": args.vad_model_path,
        "vad_threshold": args.vad_threshold,
        "vad_pre_roll_s": args.vad_pre_roll_s,
        "vad_post_roll_s": args.vad_post_roll_s,
        "vad_min_speech_s": args.vad_min_speech_s,
        "max_segment_s": args.max_segment_s,
        "listen_max_segment_s": args.listen_max_segment_s,
        "http_timeout_s": args.http_timeout_s,
        "wake_model_path": args.wake_model_path,
        "wake_verifier_path": args.wake_verifier_path,
        "wake_threshold": args.wake_threshold,
        "wake_local_spotter": args.wake_local_spotter,
        "wake_asr_model": args.wake_asr_model,
        "stuck_loop_drop": args.stuck_loop_drop,
        "stuck_loop_threshold": args.stuck_loop_threshold,
        "stream_playback": args.stream_playback,
        "live_enabled": args.live_enabled,
        "scene_model_path": args.scene_model_path,
        "scene_labels_path": args.scene_labels_path,
        "api_url": args.api_url,
        "api_key": args.api_key,
        "consent": args.consent,
        "dry_run": args.dry_run,
        "save_segments_dir": args.save_segments_dir,
        "report_interval_s": args.report_interval_s,
        "duration_s": args.duration,
        "log_level": args.log_level,
        "simulate_wav": args.simulate_wav,
        "resource_report": args.resource_report,
    }
    for name, value in overrides.items():
        if value is not None:
            setattr(cfg, name, value)
    cfg_host = (urlparse(cfg.api_url).hostname or "").lower() if cfg.api_url else ""
    if (
        cfg.api_url
        and cfg_host not in {"localhost", "127.0.0.1", "::1"}
        and not settings.ears_api_key
        and cfg.api_key == settings.master_key
    ):
        LOGGER.error(
            "remote ears delivery requires a dedicated --api-key/EV_EARS_API_KEY; "
            "master-key fallback disabled"
        )
        cfg.api_key = None
    if cfg.wake_model_path and not Path(cfg.wake_model_path).expanduser().is_file():
        fallback = (
            "using the local Whisper spotter"
            if cfg.wake_local_spotter
            else "sending VAD segments to the API for server-side spotting"
        )
        LOGGER.warning(
            "wake ONNX missing at %s; %s. Train/export the EVIE head for the "
            "lowest-latency path — see clients/ears/train/train_head.py "
            "(docs/VOICE.md).",
            cfg.wake_model_path,
            fallback,
        )
        cfg.wake_model_path = None
    cfg.device = _resolve_live_input_device(cfg.device)
    return cfg


def _device_rank(name: str) -> int:
    lower = name.lower().strip()
    if (
        "iphone" in lower
        or "continuity" in lower
        or "camera" in lower
        or lower.startswith(".")
    ):
        return 90
    if "macbook" in lower or "built-in" in lower:
        return 0
    if "airpods" in lower or "headset" in lower:
        return 8
    if "microphone" in lower:
        return 5
    return 20


def _unusable_mic(name: str) -> bool:
    return _device_rank(name) >= 90


def _builtin_mic(name: str) -> bool:
    lower = name.lower()
    return "macbook" in lower or "built-in" in lower


def _probe_input_rms(name: str) -> float:
    from app.audio.capture import probe_input_rms

    return probe_input_rms(name)


def _resolve_live_input_device(
    requested: str | None,
    *,
    probe_rms=None,
    speech_floor: float = 60.0,
) -> str | None:
    """Pick a mic without opening disconnected Bluetooth/Continuity devices.

    Opening ``Sahaj Microphone`` while it is unplugged makes macOS post
    "Audio disconnected, Sahaj's microphone is not available" on every
    probe. Built-in mics are used by name; a headset is probed only when
    it is the exact requested device.
    """

    try:
        from app.audio.capture import list_input_devices

        devices = list_input_devices()
    except Exception:
        return requested
    names = [str(item.get("name") or "") for item in devices if item.get("name")]
    if not names:
        return requested
    usable = [name for name in names if not _unusable_mic(name)]
    if not usable:
        LOGGER.warning("no usable mic; available: %s", ", ".join(names))
        return requested
    builtins = [name for name in usable if _builtin_mic(name)]
    probe = probe_rms if probe_rms is not None else _probe_input_rms

    def _use(name: str, *, why: str) -> str:
        LOGGER.info("ears using mic %s (%s)", name, why)
        return name

    if requested:
        wanted = requested.lower().strip()
        exact = [name for name in usable if name.lower() == wanted]
        if exact:
            chosen = exact[0]
            if _builtin_mic(chosen):
                return _use(chosen, why="requested built-in")
            try:
                rms = float(probe(chosen))
            except Exception:
                rms = 0.0
            LOGGER.info("mic probe name=%s rms=%.1f", chosen, rms)
            if rms >= speech_floor:
                return _use(chosen, why="requested headset live")
            if builtins:
                LOGGER.warning(
                    "mic %r is not available; using %s",
                    chosen,
                    builtins[0],
                )
                return _use(builtins[0], why="headset missing")
            return _use(chosen, why="requested, no built-in fallback")
        LOGGER.warning(
            "mic %r is not connected; available: %s",
            requested,
            ", ".join(names),
        )
        if builtins:
            return _use(builtins[0], why="requested missing")

    if builtins:
        return _use(builtins[0], why="built-in default")
    fallback = sorted(usable, key=_device_rank)[0]
    return _use(fallback, why="ranked fallback")


def ingest_http_timeout(cfg: EarConfig) -> float:
    """HTTP timeout must strictly exceed the longest clip this process posts."""

    clip = max(cfg.max_segment_s, cfg.listen_max_segment_s, cfg.wake_chunk_s)
    return max(float(cfg.http_timeout_s), clip + 15.0)


_WAKE_PREFIX = re.compile(
    r"^(?:hey|ok|okay|hi|hello)?\s*(?:evie+|eevee|evy|evi|eve|evil|every|ee\s*vee)"
    r"(?:\s+here)?\b[\s,!.?\-]*",
    re.IGNORECASE,
)


def command_after_wake(text: str) -> str:
    """Strip 'hey/hello EVIE' so the rest of the sentence is the command."""

    stripped = _WAKE_PREFIX.sub("", (text or "").strip(), count=1)
    return stripped.strip(" ,.!?")


def wake_name_hint(text: str) -> str:
    """A server-safe text hint that carries only the wake name.

    Sending a full "EVIE what's the weather" hint would make the wake request
    run the whole reply synchronously. The ears process acks the name fast and
    streams the command over SSE instead, so the hint is reduced to the name.
    """

    command = command_after_wake(text)
    if command:
        return "evie"
    return (text or "evie").strip() or "evie"


async def stream_follow_up(
    cfg: EarConfig,
    session_id: str,
    *,
    text: str | None = None,
    audio_b64: str | None = None,
    echo_hold=None,
) -> dict:
    """Stream one utterance via SSE and play each TTS chunk as it arrives.

    The server streams ``tts_chunk`` events (docs/VOICE.md §6); each chunk is
    played immediately so the owner hears the first sentence while the model
    is still writing the rest. Returns the final session state.
    """

    if not cfg.api_url:
        return {"listening": False, "reason": "no_api_url"}
    if not text and not audio_b64:
        return {"listening": False, "reason": "no_content"}
    payload: dict[str, object] = {"session_id": session_id, "follow_up": True}
    if text:
        payload["text"] = text
    else:
        payload["audio_b64"] = audio_b64
    import httpx

    headers = {"Accept": "text/event-stream"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    url = f"{cfg.api_url.rstrip('/')}/v1/voice/utterance/stream"
    timeout = ingest_http_timeout(cfg)
    reply = ""
    listening = True
    try:
        async with httpx.AsyncClient(timeout=timeout) as client, client.stream(
            "POST", url, json=payload, headers=headers
        ) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    LOGGER.warning(
                        "ears follow-up HTTP %s: %s",
                        response.status_code,
                        (body or b"")[:200],
                    )
                    return {
                        "listening": False,
                        "reason": f"http_{response.status_code}",
                    }
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    kind = event.get("type")
                    data = event.get("data") or {}
                    if kind == "tts_chunk":
                        chunk_b64 = data.get("audio_b64")
                        chunk_text = (data.get("text") or "").strip()
                        reply = reply or chunk_text
                        if chunk_b64 and cfg.stream_playback:
                            if echo_hold is not None:
                                echo_hold(True)
                            try:
                                await _play_tts(
                                    cfg, audio_b64=chunk_b64, reply=chunk_text
                                )
                            finally:
                                if echo_hold is not None:
                                    echo_hold(False)
                    elif kind == "reply":
                        reply = (data.get("reply") or reply)
                        state = data.get("state")
                        listening = bool(state in ACTIVE_SESSION_STATES)
                    elif kind == "error":
                        code = data.get("code") or ""
                        # Echo/addressivity rejections and busy states keep the
                        # session open; only a truly ended session requires a
                        # fresh wake.
                        listening = code not in {
                            "session_ended",
                            "session_expired",
                            "not_verified",
                        }
                        LOGGER.warning("ears follow-up error code=%s", code)
                    elif kind == "done":
                        break
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        LOGGER.warning("ears follow-up stream failed: %s", exc)
        listening = False
    if reply:
        _present_reply(reply)
    return {"listening": listening, "reply": reply, "session_id": session_id}


async def deliver_wake_utterance(
    cfg: EarConfig,
    *,
    frames_b64: str,
    scene: dict,
    wake_confidence: float,
    text_hint: str | None = None,
    sender=None,
    echo_hold=None,
) -> dict:
    """Send a wake-passing utterance to Agent 4's ears endpoint.

    Raw audio is sent only when ``cfg.consent`` is true and an API URL is
    configured; otherwise the call reports why nothing left the device.
    ``text_hint`` carries the on-device spotter's transcript so the server can
    (a) trust the detection and skip its own Whisper pass, and (b) extract a
    same-clip command.
    """

    if sender is not None:
        return await sender(
            cfg=cfg,
            frames_b64=frames_b64,
            scene=scene,
            wake_confidence=wake_confidence,
            text_hint=text_hint,
        )
    if not cfg.consent:
        return {"sent": False, "reason": "consent_not_granted"}
    if not cfg.api_url:
        return {"sent": False, "reason": "no_api_url"}
    if cfg.dry_run:
        return {"sent": False, "reason": "dry_run"}
    import httpx

    payload = {
        "device_id": cfg.device_id,
        "sample_rate": cfg.sample_rate,
        "wake_confidence": wake_confidence,
        "frames_b64": frames_b64,
        "scene": scene.get("scene"),
        "scene_confidence": scene.get("confidence"),
        "consent": True,
    }
    if text_hint:
        payload["text_hint"] = text_hint[:256]
        # A local spotter can recognize both the wake word and the command.
        # Keep the handshake cheap, then send only the command through the
        # streaming voice endpoint after the ack has started playing.
        if command_after_wake(text_hint) and cfg.stream_playback:
            payload["defer_command"] = True
    headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
    url = f"{cfg.api_url.rstrip('/')}/v1/ears/wake"
    timeout = ingest_http_timeout(cfg)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.TimeoutException:
        LOGGER.warning("ears ingest timed out url=%s timeout=%.1fs", url, timeout)
        return {"sent": False, "reason": "timeout", "accepted": False, "retryable": True}
    if response.status_code == 401:
        LOGGER.error(
            "ears ingest unauthorized — EV_API_KEY must match the API master key"
        )
        return {
            "sent": False,
            "status": 401,
            "url": url,
            "accepted": False,
            "listening": False,
            "reason": "unauthorized",
        }
    if response.status_code in {409, 429}:
        LOGGER.info(
            "ears ingest busy status=%s; will retry the clip",
            response.status_code,
        )
        return {
            "sent": False,
            "status": response.status_code,
            "url": url,
            "accepted": False,
            "listening": None,
            "reason": "busy",
            "retryable": True,
        }
    response.raise_for_status()
    body: dict[str, Any] = {}
    try:
        body = response.json()
    except Exception:
        body = {}
    tts = body.get("tts") if isinstance(body.get("tts"), dict) else {}
    audio_ref = tts.get("audio_ref") if tts else None
    audio_b64 = tts.get("audio_b64") if tts else None
    reply = body.get("reply") or ""
    from app.voice.speech import decide_playback

    decision = decide_playback(
        has_tts_audio=bool(audio_b64),
        audio_ref=audio_ref,
        already_played=False,
        owner=str(body.get("playback_owner") or "ears"),
        surface="ears",
    )
    if decision.play_tts or reply:
        if echo_hold is not None:
            echo_hold(True)
        try:
            if decision.play_tts:
                LOGGER.info(
                    "playing tts reply=%r bytes=%s ref=%s",
                    reply[:80],
                    bool(audio_b64),
                    audio_ref,
                )
                await _play_tts(cfg, audio_ref=audio_ref, audio_b64=audio_b64, reply=reply)
            elif reply:
                # No TTS audio, or another surface owns playback: stay
                # silent rather than switching to macOS `say`.
                LOGGER.info(
                    "skipping speak reason=%s reply=%r",
                    decision.reason,
                    reply[:80],
                )
            # Room tail after the last phoneme is still in the air.
            await asyncio.sleep(max(0.15, float(getattr(cfg, "echo_tail_s", 0.6))))
        finally:
            if echo_hold is not None:
                echo_hold(False)
        if decision.play_tts or decision.reason == "silent":
            _present_reply(reply)
    if body.get("command_deferred") and body.get("session_id") and text_hint:
        command = command_after_wake(text_hint)
        if command:
            follow = await stream_follow_up(
                cfg,
                str(body["session_id"]),
                text=command,
                echo_hold=echo_hold,
            )
            body["reply"] = follow.get("reply") or body.get("reply")
            body["listening"] = follow.get("listening", body.get("listening"))
            body["state"] = "follow_up" if body.get("listening") else "ended"
            body["transcript"] = command
    return {
        "sent": True,
        "status": response.status_code,
        "url": url,
        "accepted": body.get("accepted"),
        "listening": body.get("listening"),
        "message": body.get("message"),
        "session_id": body.get("session_id"),
        "state": body.get("state"),
        "transcript": body.get("transcript"),
        "reply": body.get("reply"),
        "audio_ref": audio_ref,
        "reason": body.get("message"),
        "command_deferred": body.get("command_deferred", False),
    }


def _present_reply(reply: str, title: str = "EVIE") -> None:
    """Glance the spoken reply as a top ticker — not a center conversation slab.

    Listen-acks (Hmm / Yes) stay audio-only. A full spoken answer already
    plays; the HUD only needs a short readable caption, JARVIS-style.
    """

    text = (reply or "").strip()
    if not text:
        return
    from app.voice.speech import LISTEN_ACKS

    if text in LISTEN_ACKS:
        return
    import subprocess
    from urllib.parse import quote, urlencode

    caption = text[:180]
    query = urlencode(
        {
            "kind": "ticker",
            "title": title,
            "body": caption,
            "time": "glance",
            "place": "top",
            "size": "ticker",
        },
        quote_via=quote,
    )
    try:
        subprocess.Popen(
            ["/usr/bin/open", f"ev://present?{query}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        LOGGER.debug("present overlay skipped: %s", exc)


async def _play_tts(
    cfg: EarConfig,
    audio_ref: str | None = None,
    audio_b64: str | None = None,
    reply: str = "",
) -> None:
    """Play synthesized speech through the user's speakers via afplay.

    There is no cross-engine fallback here on purpose: falling back to macOS
    `say` would speak with a different voice, which is the "two voices" bug.
    A playback failure is logged and the reply remains visible in the overlay.
    """

    import subprocess
    import tempfile

    try:
        raw = b""
        suffix = ".wav"
        if audio_b64:
            import base64

            raw = base64.b64decode(audio_b64)
        elif audio_ref and cfg.api_url:
            key = audio_ref
            if key.startswith("ev://"):
                key = key[len("ev://") :].lstrip("/")
            url = f"{cfg.api_url.rstrip('/')}/v1/voice/audio/{key.lstrip('/')}"
            headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
            import httpx

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, headers=headers)
            response.raise_for_status()
            raw = response.content
            if "mp3" in (response.headers.get("content-type") or ""):
                suffix = ".mp3"
        if not raw:
            raise RuntimeError("no tts audio bytes")
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            handle.write(raw)
            path = handle.name
        played = subprocess.run(
            ["/usr/bin/afplay", "-q", "1", path],
            check=False,
            capture_output=True,
            timeout=60,
        )
        with contextlib.suppress(OSError):
            Path(path).unlink(missing_ok=True)
        if played.returncode != 0:
            LOGGER.warning(
                "afplay failed rc=%s stderr=%s",
                played.returncode,
                (played.stderr or b"")[:200],
            )
        else:
            LOGGER.info("played tts bytes=%d via afplay", len(raw))
    except Exception as exc:  # noqa: BLE001 - speaking is best-effort
        LOGGER.warning("TTS playback failed: %s", exc)


def _rss_mb() -> float:
    import resource

    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value / (1024 * 1024) if sys.platform == "darwin" else value / 1024


def _cpu_seconds() -> float:
    import resource

    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def _configure_engine(cfg: EarConfig):
    """Wire real engines from config; defaults stay offline-safe."""

    if cfg.wake_model_path:
        from app.voice.wake import OpenWakeWordEngine, set_default_wake_engine

        set_default_wake_engine(
            OpenWakeWordEngine(
                model_path=cfg.wake_model_path,
                verifier_path=cfg.wake_verifier_path,
                threshold=cfg.wake_threshold,
                verifier_threshold=settings.voice_wake_openwakeword_verifier_threshold,
            )
        )
    if cfg.scene_model_path or cfg.scene_labels_path:
        set_scene_classifier(default_scene_classifier())


def default_ears_wake(cfg: EarConfig):
    """Real wake engine when configured; otherwise the local on-device spotter.

    Siri-style strictness (owner law, 2026-08-29): the always-on wake responds
    ONLY to the owner's name ("Eve"/"Evie"), never to acoustically-near words.
    The strict local faster-whisper spotter is the authoritative idle path —
    it is token-free (on-device), and only loud VAD segments reach it, so idle
    cost stays near zero on CPU and tokens. ``PhraseFallbackWake`` remains
    only for the pure-offline test double.
    """

    if cfg.wake_local_spotter and cfg.wake_strict_name:
        from clients.ears.wake import LocalWhisperWakeSpotter

        return LocalWhisperWakeSpotter(
            model=cfg.wake_asr_model or "tiny",
            threshold=cfg.wake_threshold,
        )
    if cfg.wake_model_path:
        from app.voice.wake import OpenWakeWordEngine, configured_wake_engine

        override = configured_wake_engine()
        if override.name == "openwakeword":
            return override
        return OpenWakeWordEngine(
            model_path=cfg.wake_model_path,
            verifier_path=cfg.wake_verifier_path,
            threshold=cfg.wake_threshold,
        )
    return PhraseFallbackWake()


class _SimulatedRing:
    """Disk-streaming, bounded block source for offline long-run resource checks.

    Reads one capture block at a time from a WAV file so process RSS stays
    representative of real streaming capture (no whole-file preload).
    """

    def __init__(self, path: str, block_samples: int, sample_rate: int) -> None:
        import wave

        self._wav = wave.open(path, "rb")  # noqa: SIM115 - kept open for streaming reads
        if self._wav.getnchannels() != 1 or self._wav.getsampwidth() != 2:
            self._wav.close()
            raise ValueError("simulated WAV must be mono 16-bit PCM")
        if self._wav.getframerate() != sample_rate:
            rate = self._wav.getframerate()
            self._wav.close()
            raise ValueError(f"simulated WAV must be {sample_rate} Hz, got {rate}")
        self._block_samples = max(1, block_samples)
        self.capacity = self._wav.getnframes()
        self._done = False

    def read_new(self) -> array.array:
        if self._done:
            return array.array("h")
        frames = self._wav.readframes(self._block_samples)
        if not frames:
            self._done = True
            return array.array("h")
        return array.array("h", frames)

    def read_last(self, count: int) -> array.array:
        return array.array("h", [0] * min(count, 320))

    def __len__(self) -> int:
        return self.capacity - self._wav.tell()

    def close(self) -> None:
        try:
            self._wav.close()
        finally:
            self._done = True


class _NoopStream:
    def __init__(self, ring) -> None:
        self.ring = ring

    def open(self) -> None:
        pass

    def close(self) -> None:
        ring = getattr(self, "ring", None)
        if ring is not None and hasattr(ring, "close"):
            ring.close()


async def run_ears(
    cfg: EarConfig,
    *,
    stream=None,
    wake_engine=None,
    vad_engine=None,
    scene_fn=None,
    sender=None,
    stop_event: asyncio.Event | None = None,
    require_menu_bar_app: bool = False,
    app_check_interval_s: float = 5.0,
    app_running: Callable[[], bool] | None = None,
    app_exit: Callable[[], None] | None = None,
) -> EarRunStats:
    """Run the ears loop until stopped or ``duration_s`` elapses.

    ``require_menu_bar_app`` ties the microphone to the EV menu-bar app: the
    mic is opened only while the app process is alive, and released within
    ``app_check_interval_s`` of it quitting or crashing. Tests inject
    ``app_running`` and leave the flag off for the offline paths.
    """

    stats = EarRunStats()
    stop = stop_event or asyncio.Event()
    app_alive = app_running or menu_bar_app_running
    watchdog_stop: threading.Event | None = None
    block_samples = max(1, int(cfg.sample_rate * cfg.block_ms / 1000))
    simulate = bool(cfg.simulate_wav)
    ring: Any
    if simulate and cfg.simulate_wav is not None:
        ring = _SimulatedRing(cfg.simulate_wav, block_samples, cfg.sample_rate)
        stream = _NoopStream(ring)
    elif stream is None:
        ring = PCM16RingBuffer(int(cfg.sample_rate * cfg.ring_seconds))
        stream = MicrophoneStream(
            sample_rate=cfg.sample_rate,
            block_ms=cfg.block_ms,
            device=cfg.device,
            ring=ring,
        )
    else:
        ring = stream.ring
    wake = wake_engine or default_ears_wake(cfg)
    vad = vad_engine or default_vad_engine()
    scene = scene_fn or classify_wav
    segmenter = StreamingSegmenter(
        sample_rate=cfg.sample_rate,
        pre_roll_s=cfg.vad_pre_roll_s,
        post_roll_s=cfg.vad_post_roll_s,
        min_speech_s=cfg.vad_min_speech_s,
        speech_threshold=cfg.vad_threshold,
        max_segment_s=cfg.wake_chunk_s,
    )
    save_dir = Path(cfg.save_segments_dir) if cfg.save_segments_dir else None
    if save_dir is not None:
        save_dir.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    last_report = started
    last_heartbeat = started
    last_cpu = _cpu_seconds()
    listening = False
    session_id: str | None = None
    echo_guard = False
    ingest_busy = False
    pending_segment = None
    ingest_tasks: set[asyncio.Task] = set()
    live_channel: EarsLiveChannel | None = None
    live_player: EarsLivePlayer | None = None
    live_drain: asyncio.Task | None = None
    live_sse_fallback = False
    shutting_down = False
    # Live API spotting: only when no on-device engine exists at all. With the
    # local Whisper spotter (default) or an openWakeWord head the ears process
    # detects EVIE on-device and sends the clip + confidence only on a hit,
    # instead of shipping every VAD segment to the API for a server-side pass.
    api_spotting = bool(
        cfg.consent
        and cfg.api_url
        and not cfg.dry_run
        and not cfg.wake_model_path
        and wake_engine is None
        and not cfg.wake_local_spotter
    )

    def _apply_segment_cap() -> None:
        seconds = cfg.listen_max_segment_s if listening else cfg.wake_chunk_s
        seconds = min(seconds, cfg.max_segment_s)
        segmenter.max_samples = max(1, int(seconds * cfg.sample_rate))

    def _echo_hold(on: bool) -> None:
        nonlocal echo_guard, pending_segment
        echo_guard = on
        # Playback and the post-play tail are never owner speech. Drop any
        # segment that formed on the edge of TTS so it cannot be replayed.
        pending_segment = None
        segmenter._reset()
        if not on:
            with contextlib.suppress(Exception):
                ring.read_new()

    async def _fetch_audio_ref(ref: str) -> bytes | None:
        if not cfg.api_url or not cfg.stream_playback:
            return None
        import httpx

        key = ref[len("ev://") :].lstrip("/") if ref.startswith("ev://") else ref.lstrip("/")
        url = f"{cfg.api_url.rstrip('/')}/v1/voice/audio/{key}"
        headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=headers)
        response.raise_for_status()
        return response.content

    async def _open_live(target_session_id: str) -> bool:
        """Open the persistent live door for this session, or mark SSE fallback."""

        nonlocal live_channel, live_player, live_drain, live_sse_fallback
        if shutting_down or not cfg.live_enabled or not cfg.consent or not cfg.api_url:
            return False
        if live_channel is not None and not live_channel.closed:
            return True
        try:
            channel = await EarsLiveChannel.open(
                api_url=cfg.api_url,
                session_id=target_session_id,
                api_key=cfg.api_key,
            )
        except EarsLiveUnavailable as exc:
            LOGGER.warning(
                "ears live unavailable (%s) — falling back to SSE: %s",
                exc.code,
                exc,
            )
            live_sse_fallback = True
            return False
        player = EarsLivePlayer(
            fetch_audio=_fetch_audio_ref,
            on_idle=lambda: _echo_hold(False),
        )
        await player.start()
        live_channel = channel
        live_player = player
        live_drain = asyncio.create_task(
            _drain_live(channel, player),
            name="ears-live-drain",
        )
        return True

    async def _drain_live(channel: EarsLiveChannel, player: EarsLivePlayer) -> None:
        """Consume server events for one live conversation."""

        nonlocal listening, session_id, live_channel, live_player, live_drain
        try:
            while True:
                event = await channel.receive()
                kind = str(event.get("type") or "")
                if kind in {"ready", "state"}:
                    raw_state = event.get("state")
                    state = raw_state if isinstance(raw_state, dict) else {}
                    if state.get("interruption_state") == "barged_in":
                        await player.stop()
                    continue
                if kind in {"backchannel", "tts_chunk"}:
                    audio_b64 = event.get("audio_b64")
                    audio_ref = event.get("audio_ref")
                    if cfg.stream_playback and (audio_b64 or audio_ref):
                        _echo_hold(True)
                        player.enqueue(
                            audio_b64=audio_b64,
                            audio_ref=audio_ref,
                        )
                    continue
                if kind == "reply":
                    reply = str(event.get("text") or "")
                    if reply:
                        _present_reply(reply)
                    continue
                if kind == "barge_in":
                    LOGGER.info("ears live barge-in — stopping playback")
                    await player.stop()
                    _echo_hold(False)
                    continue
                if kind == "error":
                    code = str(event.get("code") or "")
                    message = str(event.get("message") or "")
                    fatal = bool(event.get("fatal"))
                    LOGGER.warning(
                        "ears live error code=%s fatal=%s: %s", code, fatal, message
                    )
                    if code in {"session_ended", "session_expired", "not_verified"}:
                        listening = False
                        session_id = None
                    if fatal or code in {"session_ended", "session_expired"}:
                        break
                    continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - socket death is recoverable
            LOGGER.info("ears live channel closed: %s", exc)
        finally:
            await player.stop()
            await player.aclose()
            await channel.close()
            if live_channel is channel:
                live_channel = None
                live_player = None
                live_drain = None

    async def handle_segment(segment) -> None:
        nonlocal last_report, last_cpu, listening, pending_segment, session_id
        nonlocal live_channel, live_sse_fallback
        stats.segments += 1
        duration = len(segment.samples) / max(1, cfg.sample_rate)
        peak, rms = pcm_peak_rms(segment.samples)
        LOGGER.info(
            "ears chunk duration=%.2fs samples=%d rms=%.0f peak=%s listening=%s",
            duration,
            len(segment.samples),
            rms,
            peak,
            listening,
        )
        # WAKE CASCADE W1-W2: MIC→RING(10s)→VAD→Stage1(high-recall)→Stage2(precision)
        # →Speaker fast→Arbitration→Realtime→Full-utterance+directed check.
        # VAD IS NOT A HARD GATE (§4): quiet/far/hoarse owner wake must survive
        # to KWS. Reduce compute via threshold adjustment, not early drop.
        vad_quiet = not idle_clip_worth_spotting(
            segment.samples,
            min_rms=cfg.idle_min_rms,
            min_peak=cfg.idle_min_peak,
        )
        if vad_quiet:
            LOGGER.debug(
                "ears quiet chunk rms=%.0f peak=%s (need rms>=%.0f or peak>=%s) — still spotting (VAD soft gate)",
                rms,
                peak,
                cfg.idle_min_rms,
                cfg.idle_min_peak,
            )
            # Do NOT return — Stage-1 must see quiet/far-field wakes; VAD only
            # provides timing/confidence adjustment, never a drop before KWS.
        if cfg.stuck_loop_drop and await asyncio.to_thread(
            looks_stuck_loop,
            segment.samples,
            sample_rate=cfg.sample_rate,
            loop_threshold=cfg.stuck_loop_threshold,
        ):
            LOGGER.info(
                "ears drop stuck/looping chunk duration=%.2fs rms=%.0f (loop guard)",
                duration,
                rms,
            )
            return
        wav_bytes = pcm_to_wav_bytes(segment.samples, cfg.sample_rate)
        scene_result = scene(wav_bytes)
        stats.scenes[scene_result.get("scene", "unknown")] = (
            stats.scenes.get(scene_result.get("scene", "unknown"), 0) + 1
        )
        frames_b64 = base64.b64encode(pcm16_bytes(segment.samples)).decode("ascii")
        outcome: dict[str, Any] = {}
        if listening and session_id:
            # Full-duplex door: raw mic blocks already stream continuously on
            # the live socket, so this VAD segment is not re-delivered. If the
            # door is not open yet (or the server refused it), fall back to
            # one clip per SSE request.
            if live_channel is not None and not live_channel.closed:
                stats.utterances_sent += 1
            elif cfg.live_enabled and not live_sse_fallback and await _open_live(
                str(session_id)
            ):
                channel = live_channel
                if channel is not None:
                    await channel.send_audio_segment(pcm16_bytes(segment.samples))
                stats.utterances_sent += 1
            else:
                wav_b64 = base64.b64encode(wav_bytes).decode("ascii")
                outcome = await stream_follow_up(
                    cfg,
                    session_id,
                    audio_b64=wav_b64,
                    echo_hold=_echo_hold,
                )
                if not outcome.get("listening"):
                    session_id = None
                    listening = False
                else:
                    stats.utterances_sent += 1
        elif not api_spotting and not listening:
            detection = await wake.detect(
                frames=pcm16_bytes(segment.samples),
                sample_rate=cfg.sample_rate,
                device_id=cfg.device_id,
            )
            if not detection.triggered:
                # A missing local model must not turn into a silent listener.
                # Let a configured server-side engine (Porcupine, an exported
                # head, or a different ASR runtime) make the decision instead.
                if not (
                    (detection.details or {}).get("error")
                    and cfg.consent
                    and cfg.api_url
                    and not cfg.dry_run
                ):
                    return
                outcome = await deliver_wake_utterance(
                    cfg,
                    frames_b64=frames_b64,
                    scene=scene_result,
                    wake_confidence=0.0,
                    sender=sender,
                    echo_hold=_echo_hold,
                )
                if outcome.get("accepted"):
                    stats.wake_hits += 1
                if outcome.get("session_id"):
                    session_id = str(outcome["session_id"])
                listening = bool(outcome.get("listening"))
                _apply_segment_cap()
                return
            stats.wake_hits += 1
            # WAKE CASCADE W1-W4: Stage1(high-recall, already passed) → Stage2
            # (high-precision second-pass, runs only after Stage1 candidate so
            # large verifier/SpeakerID are not resident while idle; §22 budget).
            # Architecture chooses JOB, evidence chooses MODEL — benchmark smallest
            # reliable verifier, do NOT hardcode Conformer/CTC/Whisper.
            try:
                # Stage-2: when the head is present its verifier (threshold 0.3)
                # already ran inside OpenWakeWordEngine; for the Whisper spotter
                # we have no verifier pkl — verify transcript head-anchored.
                engine_name = str((detection.details or {}).get("engine") or "")
                if engine_name == "whisper-spotter" and not (detection.details or {}).get("transcript"):
                    # Whisper fired but transcript empty → low precision, keep
                    # as candidate but mark verifier false (will be recheckd
                    # server-side with ASR + full-utterance).
                    LOGGER.debug("wake Stage-2: whisper spotter no transcript — deferring to server ASR")
            except Exception:  # noqa: BLE001 - Stage-2 must not crash wake
                pass
            # Speaker fast + Directed + Arbitration groundwork are server-side
            # authoritative (lifecycle.handle_ears_ingest does fast wake-phrase
            # confidence → full-utterance recheck, directed-speech, and
            # ConversationLease arbitration with deterministic factors). Local
            # pre-filter: drop obvious non-directed before upload to save
            # bandwidth, but never fabricate an action — cancel before meaningful
            # execution, bounded diagnostics only.
            try:
                _heard_tmp = (detection.details or {}).get("transcript") or ""
                # Light local directed pre-filter (authoritative check is server-side)
                # "Evie is..." and "Did you see Evie?" must not trigger upload.
                _lower = _heard_tmp.strip().lower()
                if _lower and not _lower.lstrip().startswith(("evie", "hey evie", "hi evie", "ok evie", "hello evie")):
                    # Not anchored at head → likely conversational mention
                    if "evie" in _lower and not _lower.strip().startswith("evie"):
                        LOGGER.info("ears local directed pre-filter: not anchored — skipping upload (server will also cancel)")
                        return
            except Exception:  # noqa: BLE001
                pass
            heard = (detection.details or {}).get("transcript") or ""
            command = command_after_wake(heard)
            # The wake request acks the name fast (server trusts on-device
            # confidence and skips its own Whisper pass). A same-clip command
            # streams as a follow-up so audio starts before the model finishes.
            outcome = await deliver_wake_utterance(
                cfg,
                frames_b64=frames_b64,
                scene=scene_result,
                wake_confidence=detection.confidence,
                text_hint=wake_name_hint(heard),
                sender=sender,
                echo_hold=_echo_hold,
            )
            if outcome.get("session_id"):
                session_id = str(outcome["session_id"])
            listening = bool(outcome.get("listening"))
            _apply_segment_cap()
            if command and session_id and listening:
                LOGGER.info("ears wake+command streaming: %r", command[:80])
                if cfg.live_enabled and await _open_live(str(session_id)):
                    channel = live_channel
                    if channel is not None:
                        await channel.send_text(command)
                    stats.utterances_sent += 1
                else:
                    follow = await stream_follow_up(
                        cfg,
                        session_id,
                        text=command,
                        echo_hold=_echo_hold,
                    )
                    if not follow.get("listening"):
                        session_id = None
                        listening = False
                    else:
                        stats.utterances_sent += 1
            elif not heard and session_id and listening:
                # openWakeWord supplies a hit score but no transcript. Reuse
                # the captured WAV as the command clip so "EVIE, do X" is not
                # reduced to an acknowledgment and discarded.
                LOGGER.info("ears wake hit had no transcript; streaming captured clip for ASR")
                if cfg.live_enabled and await _open_live(str(session_id)):
                    channel = live_channel
                    if channel is not None:
                        await channel.send_audio_segment(pcm16_bytes(segment.samples))
                    stats.utterances_sent += 1
                else:
                    follow = await stream_follow_up(
                        cfg,
                        session_id,
                        audio_b64=base64.b64encode(wav_bytes).decode("ascii"),
                        echo_hold=_echo_hold,
                    )
                    if not follow.get("listening"):
                        session_id = None
                        listening = False
                    else:
                        stats.utterances_sent += 1
        else:
            # Idle API spotting fallback (no on-device engine): send every
            # segment and let the server spot EVIE.
            outcome = await deliver_wake_utterance(
                cfg,
                frames_b64=frames_b64,
                scene=scene_result,
                wake_confidence=1.0 if listening else 0.0,
                sender=sender,
                echo_hold=_echo_hold,
            )
            if outcome.get("accepted"):
                stats.wake_hits += 1
            if outcome.get("session_id"):
                session_id = str(outcome["session_id"])
        if outcome.get("retryable") or outcome.get("reason") in {"timeout", "busy"}:
            if pending_segment is None or pcm_peak_rms(segment.samples)[1] >= pcm_peak_rms(
                pending_segment.samples
            )[1]:
                pending_segment = segment
        elif outcome.get("listening") is not None:
            listening = bool(outcome.get("listening"))
            _apply_segment_cap()
        if outcome.get("sent"):
            stats.utterances_sent += 1
        LOGGER.info(
            "ears ingest accepted=%s listening=%s state=%s sent=%s reason=%s reply=%s",
            outcome.get("accepted"),
            outcome.get("listening"),
            outcome.get("state"),
            outcome.get("sent"),
            outcome.get("reason") or outcome.get("message"),
            (outcome.get("reply") or "")[:80],
        )
        if save_dir is not None:
            path = save_dir / f"wake-{int(time.time() * 1000)}.wav"
            path.write_bytes(wav_bytes)
            LOGGER.info("debug segment saved (opt-in only): %s", path)
        now = time.monotonic()
        if now - last_report >= cfg.report_interval_s:
            cpu = _cpu_seconds()
            elapsed = now - last_report
            avg_cpu = (cpu - last_cpu) / elapsed if elapsed > 0 else 0.0
            LOGGER.info(
                "report blocks=%d segments=%d wake=%d sent=%d rss=%.1fMB avg_cpu=%.2f%% "
                "ring_fill=%d/%d",
                stats.blocks,
                stats.segments,
                stats.wake_hits,
                stats.utterances_sent,
                _rss_mb(),
                avg_cpu * 100.0,
                len(ring),
                ring.capacity,
            )
            last_report = now
            last_cpu = cpu

    def _spawn_segment(segment) -> None:
        nonlocal ingest_busy, pending_segment
        if ingest_busy:
            if pending_segment is None or pcm_peak_rms(segment.samples)[1] >= pcm_peak_rms(
                pending_segment.samples
            )[1]:
                pending_segment = segment
            return
        ingest_busy = True
        task = asyncio.create_task(handle_segment(segment))
        ingest_tasks.add(task)

        def _done(done: asyncio.Task) -> None:
            nonlocal ingest_busy, pending_segment
            ingest_tasks.discard(done)
            if not done.cancelled() and done.exception() is not None:
                LOGGER.error("ears ingest task: %s", done.exception())
            ingest_busy = False
            if pending_segment is not None:
                nxt = pending_segment
                pending_segment = None
                _spawn_segment(nxt)

        task.add_done_callback(_done)

    if require_menu_bar_app and not app_alive():
        LOGGER.warning(
            "ears: EV menu-bar app is not running; microphone not opened"
        )
        return stats
    if not await wait_for_mic_ownership():
        # EV.app's live session owns the input (accepted-wake handoff).
        # ONE mic owner applies in every wake mode; exit without touching
        # the mic and let launchd re-run this check.
        return stats
    if require_menu_bar_app:
        # Hard guarantee: release the mic even if the main loop is stuck in a
        # long HTTP/TTS call and never gets around to the periodic in-loop check.
        watchdog_stop = start_app_watchdog(
            app_alive, interval_s=app_check_interval_s, on_exit=app_exit
        )
    # Warm the wake model before opening the mic. Otherwise the first spoken
    # "EVIE" pays a model download + load (~15 s) and reads as a missed wake.
    warmup: Any = getattr(wake, "warmup", None)
    if warmup is not None:
        try:
            await warmup()
        except Exception as exc:  # noqa: BLE001 - warmup is best-effort
            LOGGER.warning("wake warmup failed (continuing): %s", exc)
    try:
        stream.open()
    except MicrophoneDeniedError as exc:
        LOGGER.error("Microphone permission denied: %s", exc)
        LOGGER.error(
            "Fix: System Settings > Privacy & Security > Microphone, enable this "
            "app/terminal, then restart ears. Never failing silently."
        )
        return stats
    except MicrophoneUnavailableError as exc:
        LOGGER.error("Microphone unavailable: %s", exc)
        return stats

    LOGGER.info(
        "ears started device=%s rate=%d ring=%.1fs wake=%s vad=%s "
        "consent=%s dry_run=%s wake_chunk=%.1fs idle_rms=%.0f idle_peak=%s",
        cfg.device or "default",
        cfg.sample_rate,
        cfg.ring_seconds,
        wake.name,
        vad.name,
        cfg.consent,
        cfg.dry_run,
        cfg.wake_chunk_s,
        cfg.idle_min_rms,
        cfg.idle_min_peak,
    )
    async def run_loop() -> None:
        nonlocal last_heartbeat
        consecutive_errors = 0
        last_app_check = time.monotonic()
        while not stop.is_set():
            if time.monotonic() - last_app_check >= app_check_interval_s:
                last_app_check = time.monotonic()
                if require_menu_bar_app and not app_alive():
                    LOGGER.warning(
                        "ears: EV menu-bar app is not running; releasing microphone"
                    )
                    break
                if ev_live_owns_mic():
                    # A live session started while ears was already running.
                    # ONE mic owner (every wake mode): release the input; the
                    # post-exit respawn waits on the marker before reopening.
                    LOGGER.warning(
                        "ears: EV.app live session owns the mic; releasing"
                    )
                    break
            if cfg.duration_s is not None and time.monotonic() - started >= cfg.duration_s:
                break
            try:
                block = ring.read_new()
                now = time.monotonic()
                if now - last_heartbeat >= 10.0:
                    peak = max((abs(int(s)) for s in block), default=0) if block else 0
                    rms = (
                        (sum(int(s) * int(s) for s in block) / len(block)) ** 0.5
                        if block
                        else 0.0
                    )
                    LOGGER.info(
                        "ears heartbeat blocks=%d segments=%d listening=%s "
                        "device=%s ring=%d peak=%s rms=%.0f busy=%s pending=%s",
                        stats.blocks,
                        stats.segments,
                        listening,
                        cfg.device or "default",
                        len(ring),
                        peak,
                        rms,
                        ingest_busy,
                        pending_segment is not None,
                    )
                    last_heartbeat = now
                if not block:
                    await asyncio.sleep(0.01)
                    continue
                stats.blocks += 1
                if simulate:
                    await asyncio.sleep(cfg.block_ms / 1000)
                if echo_guard:
                    continue
                channel = live_channel
                if channel is not None and not channel.closed:
                    # Full-duplex: the server owns turn-taking now. The local
                    # segmenter below still watches for wake in idle mode, but
                    # an open live door receives every block unmodified.
                    channel.offer_pcm(pcm16_bytes(block))
                try:
                    probability = await vad.block_probability(block, cfg.sample_rate)
                except Exception as exc:  # model failure → degrade, never crash loop
                    LOGGER.warning("VAD error, using silence decision: %s", exc)
                    probability = 0.0
                pre_roll = None
                if not segmenter.active:
                    window = ring.read_last(cfg.pre_roll_samples + len(block))
                    extra = max(0, len(window) - len(block))
                    if extra:
                        pre_roll = window[:extra]
                segment = segmenter.push(block, probability, pre_roll_samples=pre_roll)
                if segment is None:
                    continue
                _spawn_segment(segment)
                consecutive_errors = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                consecutive_errors += 1
                LOGGER.error(
                    "ears loop error (%d): %s: %s",
                    consecutive_errors,
                    type(exc).__name__,
                    exc,
                )
                if consecutive_errors > 10:
                    LOGGER.error("too many consecutive errors; giving up")
                    break
                await asyncio.sleep(2.0)

    try:
        await run_loop()
    finally:
        # Stop the watchdog thread before it can hard-exit the host process on
        # a later timeline (the app-runs check would fire os._exit once the
        # injected ``app_running`` flips false — this is what kills pytest on
        # the *next* test if the thread is left running).
        if watchdog_stop is not None:
            watchdog_stop.set()
        # Release the microphone the instant the loop ends — never wait for
        # in-flight ingestion/TTS before closing the mic, or it would appear
        # "on" for up to a full HTTP timeout after EV quits.
        stream.close()
    if ingest_tasks:
        await asyncio.gather(*ingest_tasks, return_exceptions=True)
    shutting_down = True
    channel = live_channel
    if channel is not None:
        await channel.close()
    if live_player is not None:
        await live_player.aclose()
    if live_drain is not None:
        await asyncio.gather(live_drain, return_exceptions=True)
    tail = segmenter.flush()
    if tail is not None:
        await handle_segment(tail)
    if cfg.resource_report:
        wall = max(1e-6, time.monotonic() - started)
        cpu = _cpu_seconds() - last_cpu
        report = {
            "rss_max_mb": round(_rss_mb(), 2),
            "cpu_seconds": round(cpu, 3),
            "wall_seconds": round(wall, 3),
            "avg_cpu_fraction": round(cpu / wall, 4),
            "blocks": stats.blocks,
            "segments": stats.segments,
            "wake_hits": stats.wake_hits,
            "simulate_wav": cfg.simulate_wav,
            "bounded": {
                "simulated_source_samples": getattr(ring, "capacity", 0),
                "max_segment_samples": segmenter.max_samples,
            },
        }
        target = Path(cfg.resource_report)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        LOGGER.info("resource report written to %s", target)
    LOGGER.info("ears stopped: %s", stats)
    return stats


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m clients.ears",
        description="EVIE always-on ears process (mic → VAD → wake → scene → Agent 4).",
    )
    parser.add_argument("--device", help="PortAudio input device name or index")
    parser.add_argument("--list-devices", action="store_true", help="list input devices and exit")
    parser.add_argument("--sample-rate", type=int, default=None)
    parser.add_argument("--ring-seconds", type=float, default=None)
    parser.add_argument("--block-ms", type=int, default=None)
    parser.add_argument("--device-id", default=None)
    parser.add_argument("--vad-model-path", default=None)
    parser.add_argument("--vad-threshold", type=float, default=None)
    parser.add_argument("--vad-pre-roll-s", type=float, default=None)
    parser.add_argument("--vad-post-roll-s", type=float, default=None)
    parser.add_argument("--vad-min-speech-s", type=float, default=None)
    parser.add_argument("--max-segment-s", type=float, default=None)
    parser.add_argument("--listen-max-segment-s", type=float, default=None)
    parser.add_argument("--http-timeout-s", type=float, default=None)
    parser.add_argument("--wake-model-path", default=None)
    parser.add_argument("--wake-verifier-path", default=None)
    parser.add_argument("--wake-threshold", type=float, default=None)
    parser.add_argument("--wake-local-spotter", dest="wake_local_spotter", action="store_true", default=None)
    parser.add_argument("--no-wake-local-spotter", dest="wake_local_spotter", action="store_false")
    parser.add_argument("--wake-asr-model", default=None)
    parser.add_argument("--stuck-loop-drop", dest="stuck_loop_drop", action="store_true", default=None)
    parser.add_argument("--no-stuck-loop-drop", dest="stuck_loop_drop", action="store_false")
    parser.add_argument("--stuck-loop-threshold", type=float, default=None)
    parser.add_argument("--stream-playback", dest="stream_playback", action="store_true", default=None)
    parser.add_argument("--no-stream-playback", dest="stream_playback", action="store_false")
    parser.add_argument("--live", dest="live_enabled", action="store_true", default=None)
    parser.add_argument("--no-live", dest="live_enabled", action="store_false")
    parser.add_argument("--scene-model-path", default=None)
    parser.add_argument("--scene-labels-path", default=None)
    parser.add_argument("--api-url", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--consent", action="store_true", default=None)
    parser.add_argument("--no-consent", dest="consent", action="store_false")
    parser.add_argument("--dry-run", action="store_true", default=None)
    parser.add_argument("--save-segments-dir", default=None)
    parser.add_argument("--report-interval-s", type=float, default=None)
    parser.add_argument("--duration", type=float, default=None, help="run for N seconds, then exit")
    parser.add_argument("--log-level", default=None)
    parser.add_argument("--simulate-wav", default=None, help="offline long-run mode: pace a WAV at real time")
    parser.add_argument("--resource-report", default=None, help="write RSS/CPU JSON at shutdown")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.list_devices:
        try:
            for device in list_input_devices():
                print(
                    f"[{device['index']}] {device['name']} "
                    f"(default {device['default_samplerate']} Hz, "
                    f"{device['max_input_channels']}ch)"
                )
        except (MicrophoneDeniedError, MicrophoneUnavailableError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        return 0
    cfg = build_config(args)
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _configure_engine(cfg)

    async def _run() -> int:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        # WAKE V1 app-less law: when ALWAYS_AVAILABLE_WAKE is SHADOW/ON, EARS must
        # run without a visible EV.app window (lightweight background listener at
        # login). OFF keeps the old tied-to-menu-bar behavior for rollback.
        from app.config import settings as _settings

        _mode = (_settings.always_available_wake or "OFF").strip().upper()
        _require_app = _mode == "OFF"
        stats = await run_ears(
            cfg,
            stop_event=stop,
            require_menu_bar_app=_require_app,
        )
        return 0 if stats.blocks else 3

    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
