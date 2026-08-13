"""Always-on ears process: mic → ring → VAD → wake → scene → Agent 4."""

from __future__ import annotations

import argparse
import array
import asyncio
import base64
import contextlib
import json
import logging
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.audio.capture import (
    MicrophoneDeniedError,
    MicrophoneStream,
    MicrophoneUnavailableError,
    list_input_devices,
    pcm_to_wav_bytes,
)
from app.audio.ring import PCM16RingBuffer, pcm16_bytes
from app.audio.scene import classify_wav, default_scene_classifier, set_scene_classifier
from app.audio.vad import StreamingSegmenter, default_vad_engine
from app.config import settings
from clients.ears.wake import PhraseFallbackWake

LOGGER = logging.getLogger("ears")


@dataclass
class EarConfig:
    device: str | None = None
    sample_rate: int = 16000
    ring_seconds: float = 10.0
    block_ms: int = 20
    device_id: str = "mac-ears"
    vad_model_path: str | None = None
    vad_threshold: float = 0.5
    vad_pre_roll_s: float = 0.25
    vad_post_roll_s: float = 0.75
    vad_min_speech_s: float = 0.2
    max_segment_s: float = 60.0
    wake_model_path: str | None = None
    wake_verifier_path: str | None = None
    wake_threshold: float = 0.5
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


def build_config(args: argparse.Namespace | None = None) -> EarConfig:
    """Config from settings + CLI flags (CLI wins)."""

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
        wake_model_path=settings.voice_wake_openwakeword_model_path,
        wake_verifier_path=settings.voice_wake_openwakeword_verifier_path,
        wake_threshold=settings.ears_wake_threshold,
        scene_model_path=settings.ears_scene_model_path,
        scene_labels_path=settings.ears_scene_labels_path,
        api_url=settings.ears_api_url,
        api_key=settings.ears_api_key,
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
        "wake_model_path": args.wake_model_path,
        "wake_verifier_path": args.wake_verifier_path,
        "wake_threshold": args.wake_threshold,
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
    return cfg


async def deliver_wake_utterance(
    cfg: EarConfig,
    *,
    frames_b64: str,
    scene: dict,
    wake_confidence: float,
    sender=None,
) -> dict:
    """Send a wake-passing utterance to Agent 4's ears endpoint.

    Raw audio is sent only when ``cfg.consent`` is true and an API URL is
    configured; otherwise the call reports why nothing left the device.
    """

    if sender is not None:
        return await sender(
            cfg=cfg, frames_b64=frames_b64, scene=scene, wake_confidence=wake_confidence
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
    headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
    url = f"{cfg.api_url.rstrip('/')}/v1/ears/wake"
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, json=payload, headers=headers)
    response.raise_for_status()
    return {"sent": True, "status": response.status_code, "url": url}


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


def _vosk_model_on_disk() -> bool:
    """True when the Vosk model directory is present — no voice-stack import.

    Importing ``app.voice`` pulls in the lifecycle and blows the ears process
    RSS budget, so the default path checks the filesystem first and only loads
    the real engine when the model is actually there.
    """

    configured = settings.voice_vosk_model_path
    path = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".ev" / "models" / "vosk-model-small-en-us-0.15"
    )
    return (path / "conf" / "model.conf").is_file()


def default_ears_wake(cfg: EarConfig):
    """Real wake engine when a model is on disk; otherwise the light offline double."""

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
    if _vosk_model_on_disk():
        try:
            import vosk  # noqa: F401
        except ImportError:
            return PhraseFallbackWake()
        from app.voice.vosk_engine import VoskWakeEngine

        return VoskWakeEngine(threshold=cfg.wake_threshold)
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
) -> EarRunStats:
    """Run the ears loop until stopped or ``duration_s`` elapses."""

    stats = EarRunStats()
    stop = stop_event or asyncio.Event()
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
        max_segment_s=cfg.max_segment_s,
    )
    save_dir = Path(cfg.save_segments_dir) if cfg.save_segments_dir else None
    if save_dir is not None:
        save_dir.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    last_report = started
    last_cpu = _cpu_seconds()

    async def handle_segment(segment) -> None:
        nonlocal last_report, last_cpu
        stats.segments += 1
        detection = await wake.detect(
            frames=pcm16_bytes(segment.samples),
            sample_rate=cfg.sample_rate,
            device_id=cfg.device_id,
        )
        if not detection.triggered:
            return
        stats.wake_hits += 1
        wav_bytes = pcm_to_wav_bytes(segment.samples, cfg.sample_rate)
        scene_result = scene(wav_bytes)
        stats.scenes[scene_result.get("scene", "unknown")] = (
            stats.scenes.get(scene_result.get("scene", "unknown"), 0) + 1
        )
        frames_b64 = base64.b64encode(pcm16_bytes(segment.samples)).decode("ascii")
        outcome = await deliver_wake_utterance(
            cfg,
            frames_b64=frames_b64,
            scene=scene_result,
            wake_confidence=detection.confidence,
            sender=sender,
        )
        if outcome.get("sent"):
            stats.utterances_sent += 1
        LOGGER.info(
            "wake hit confidence=%.3f scene=%s sent=%s reason=%s",
            detection.confidence,
            scene_result.get("scene"),
            outcome.get("sent"),
            outcome.get("reason"),
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
        "ears started device=%s rate=%d ring=%.1fs wake=%s vad=%s consent=%s dry_run=%s",
        cfg.device or "default",
        cfg.sample_rate,
        cfg.ring_seconds,
        wake.name,
        vad.name,
        cfg.consent,
        cfg.dry_run,
    )
    async def run_loop() -> None:
        consecutive_errors = 0
        while not stop.is_set():
            if cfg.duration_s is not None and time.monotonic() - started >= cfg.duration_s:
                break
            try:
                block = ring.read_new()
                if not block:
                    await asyncio.sleep(0.01)
                    continue
                stats.blocks += 1
                if simulate:
                    await asyncio.sleep(cfg.block_ms / 1000)
                try:
                    probability = await vad.block_probability(block, cfg.sample_rate)
                except Exception as exc:  # model failure → degrade, never crash loop
                    LOGGER.warning("VAD error, using silence decision: %s", exc)
                    probability = 0.0
                pre_roll = ring.read_last(cfg.pre_roll_samples) if not segmenter.active else None
                segment = segmenter.push(block, probability, pre_roll_samples=pre_roll)
                if segment is None:
                    continue
                await handle_segment(segment)
                consecutive_errors = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                consecutive_errors += 1
                LOGGER.error("ears loop error (%d): %s", consecutive_errors, exc)
                if consecutive_errors > 10:
                    LOGGER.error("too many consecutive errors; giving up")
                    break
                await asyncio.sleep(2.0)

    try:
        await run_loop()
    finally:
        stream.close()
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
    parser.add_argument("--wake-model-path", default=None)
    parser.add_argument("--wake-verifier-path", default=None)
    parser.add_argument("--wake-threshold", type=float, default=None)
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
        stats = await run_ears(cfg, stop_event=stop)
        return 0 if stats.blocks else 3

    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
