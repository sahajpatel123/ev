"""Always-on "EVIE" listener: microphone → /v1/voice/live → speaker.

A thin transport. Every decision (wake, endpointing, follow-up, barge-in) lives
server-side in :mod:`app.voice.live`, so this client and the web workbench and
the macOS app all behave identically.

    uv run python -m clients.hands_free --api-url http://127.0.0.1:8000 \
        --api-key "$EV_MASTER_KEY"

``--simulate-wav`` replaces the microphone with a WAV file paced at real time,
which is how the loop is exercised on machines with no audio hardware.
"""

from __future__ import annotations

import argparse
import array
import asyncio
import base64
import contextlib
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import wave
from dataclasses import dataclass

LOGGER = logging.getLogger("hands_free")

STATE_LABELS = {
    "idle": 'listening for "EVIE"',
    "waking": "heard you",
    "listening": "listening",
    "thinking": "thinking",
    "speaking": "speaking",
    "follow_up": "go ahead (no wake word needed)",
    "closed": "closed",
}


@dataclass
class ClientConfig:
    api_url: str
    api_key: str
    device_id: str = "mac-hands-free"
    device: str | int | None = None
    sample_rate: int = 16000
    frame_ms: int = 20
    simulate_wav: str | None = None
    play_audio: bool = True
    duration_s: float | None = None
    log_level: str = "INFO"

    @property
    def ws_url(self) -> str:
        base = self.api_url.rstrip("/")
        if base.startswith("https://"):
            return "wss://" + base[len("https://") :] + "/v1/voice/live"
        if base.startswith("http://"):
            return "ws://" + base[len("http://") :] + "/v1/voice/live"
        return base + "/v1/voice/live"

    @property
    def frame_samples(self) -> int:
        return max(1, int(self.sample_rate * self.frame_ms / 1000))


# --------------------------------------------------------------------------- #
# Audio sources
# --------------------------------------------------------------------------- #


class MicrophoneSource:
    """Blocking sounddevice input stream drained from the event loop."""

    name = "microphone"

    def __init__(self, config: ClientConfig) -> None:
        self.config = config
        self._stream = None
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
        self._loop: asyncio.AbstractEventLoop | None = None

    def open(self) -> None:
        try:
            import sounddevice
        except OSError as exc:  # PortAudio missing
            raise RuntimeError(
                "PortAudio is unavailable; install it (macOS: brew install portaudio) "
                "and the 'mic' extra: uv sync --extra mic"
            ) from exc
        except ImportError as exc:
            raise RuntimeError(
                "sounddevice is not installed; run: uv sync --extra mic"
            ) from exc

        self._loop = asyncio.get_running_loop()

        def callback(indata, frames, time_info, status) -> None:  # noqa: ARG001
            if status:
                LOGGER.debug("input stream status: %s", status)
            payload = bytes(indata)
            loop = self._loop
            if loop is None:
                return
            try:
                loop.call_soon_threadsafe(self._queue.put_nowait, payload)
            except (asyncio.QueueFull, RuntimeError):
                LOGGER.warning("dropping a mic frame: consumer is behind")

        self._stream = sounddevice.RawInputStream(
            samplerate=self.config.sample_rate,
            blocksize=self.config.frame_samples,
            device=self.config.device,
            channels=1,
            dtype="int16",
            callback=callback,
        )
        self._stream.start()

    async def frames(self):
        while True:
            yield await self._queue.get()

    def close(self) -> None:
        if self._stream is not None:
            with contextlib.suppress(Exception):
                self._stream.stop()
                self._stream.close()
            self._stream = None


class WavSource:
    """WAV file paced at real time, for machines with no microphone."""

    name = "simulated-wav"

    def __init__(self, config: ClientConfig, path: str) -> None:
        self.config = config
        self.path = path
        self._wav: wave.Wave_read | None = None

    def open(self) -> None:
        handle = wave.open(self.path, "rb")  # noqa: SIM115 - held open for streaming reads
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            handle.close()
            raise RuntimeError("simulated WAV must be mono 16-bit PCM")
        if handle.getframerate() != self.config.sample_rate:
            rate = handle.getframerate()
            handle.close()
            raise RuntimeError(
                f"simulated WAV must be {self.config.sample_rate} Hz, got {rate}"
            )
        self._wav = handle

    async def frames(self):
        assert self._wav is not None
        frame = self.config.frame_samples
        period = self.config.frame_ms / 1000
        started = time.monotonic()
        index = 0
        while True:
            data = self._wav.readframes(frame)
            if not data:
                # Keep the stream alive with silence so follow-up windows and
                # timeouts play out exactly as they would on a live mic.
                data = array.array("h", [0] * frame).tobytes()
            index += 1
            yield data
            delay = started + index * period - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)

    def close(self) -> None:
        if self._wav is not None:
            with contextlib.suppress(Exception):
                self._wav.close()
            self._wav = None


# --------------------------------------------------------------------------- #
# Playback
# --------------------------------------------------------------------------- #


async def play_wav_bytes(audio: bytes) -> None:
    """Play reply audio on the default output device.

    sounddevice when available (no temp file, exact duration), otherwise the
    platform player. Failure is logged, never fatal: a client that cannot play
    audio must still keep listening.
    """

    try:
        import numpy  # noqa: F401
        import sounddevice
    except Exception:
        await _play_with_platform_player(audio)
        return
    try:
        import io

        import numpy as np

        with wave.open(io.BytesIO(audio), "rb") as handle:
            frames = handle.readframes(handle.getnframes())
            rate = handle.getframerate()
            channels = handle.getnchannels()
        samples = np.frombuffer(frames, dtype=np.int16)
        if channels > 1:
            samples = samples.reshape(-1, channels)
        await asyncio.to_thread(sounddevice.play, samples, rate, blocking=True)
    except Exception as exc:  # noqa: BLE001 - fall back, never crash the loop
        LOGGER.warning("sounddevice playback failed (%s); trying the platform player", exc)
        await _play_with_platform_player(audio)


async def _play_with_platform_player(audio: bytes) -> None:
    player = next(
        (name for name in ("afplay", "paplay", "aplay", "ffplay") if shutil.which(name)),
        None,
    )
    if player is None:
        LOGGER.warning("no audio player available; reply audio was not played")
        return
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
        handle.write(audio)
        path = handle.name
    argv = [player, path]
    if player == "ffplay":
        argv = [player, "-nodisp", "-autoexit", "-loglevel", "quiet", path]
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        await process.wait()
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


def speak_locally(text: str) -> None:
    """Speak a reply with the platform voice when the server has no TTS."""

    if sys.platform == "darwin" and shutil.which("say"):
        with contextlib.suppress(Exception):
            subprocess.Popen(["say", text])
        return
    LOGGER.info("EVIE (no audio available): %s", text)


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #


class HandsFreeClient:
    def __init__(self, config: ClientConfig) -> None:
        self.config = config
        self.source = (
            WavSource(config, config.simulate_wav)
            if config.simulate_wav
            else MicrophoneSource(config)
        )
        self.turns = 0
        self.wakes = 0

    async def run(self, stop: asyncio.Event) -> int:
        import websockets

        backoff = 1.0
        started = time.monotonic()
        while not stop.is_set():
            if self.config.duration_s and time.monotonic() - started >= self.config.duration_s:
                return 0
            try:
                async with websockets.connect(
                    self.config.ws_url, max_size=32 * 1024 * 1024
                ) as socket:
                    backoff = 1.0
                    await self._session(socket, stop, started)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect, never exit on a blip
                if stop.is_set():
                    break
                LOGGER.error("live stream error (%s); reconnecting in %.0fs", exc, backoff)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                backoff = min(backoff * 2, 30.0)
        return 0

    async def _session(self, socket, stop: asyncio.Event, started: float) -> None:
        await socket.send(
            json.dumps(
                {
                    "type": "auth",
                    "token": self.config.api_key,
                    "device_id": self.config.device_id,
                }
            )
        )
        sender = asyncio.create_task(self._send_audio(socket, stop, started))
        try:
            async for raw in socket:
                if isinstance(raw, bytes):
                    continue
                await self._handle_event(socket, json.loads(raw))
        finally:
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender

    async def _send_audio(self, socket, stop: asyncio.Event, started: float) -> None:
        self.source.open()
        LOGGER.info(
            "listening on %s (%s) — say \"EVIE\"",
            self.source.name,
            self.config.device if self.config.device is not None else "default device",
        )
        try:
            async for frame in self.source.frames():
                if stop.is_set():
                    return
                if self.config.duration_s and time.monotonic() - started >= self.config.duration_s:
                    stop.set()
                    return
                await socket.send(frame)
        finally:
            self.source.close()

    async def _handle_event(self, socket, event: dict) -> None:
        kind = event.get("type")
        data = event.get("data") or {}
        if kind == "ready":
            if not data.get("ready", False):
                LOGGER.error("EVIE cannot hear: %s", " | ".join(data.get("blockers", [])))
                return
            LOGGER.info(
                "engines ready: wake=%s asr=%s tts=%s",
                data.get("wake", {}).get("engine"),
                data.get("asr", {}).get("provider"),
                data.get("tts", {}).get("provider"),
            )
        elif kind == "state":
            LOGGER.info("[%s]", STATE_LABELS.get(data.get("state"), data.get("state")))
        elif kind == "wake":
            if data.get("stage") == "confirmed":
                self.wakes += 1
                LOGGER.info("wake confirmed: %r (%.2f)", data.get("phrase"), data.get("confidence", 0.0))
        elif kind == "partial":
            LOGGER.debug("… %s", data.get("text"))
        elif kind == "transcript":
            self.turns += 1
            LOGGER.info("you: %s", data.get("text"))
        elif kind == "reply":
            LOGGER.info("EVIE: %s", data.get("text"))
            if data.get("speak_locally"):
                speak_locally(data.get("text") or "")
                await socket.send(json.dumps({"type": "playback_finished"}))
        elif kind == "audio":
            payload = data.get("audio_b64")
            if payload and self.config.play_audio:
                await play_wav_bytes(base64.b64decode(payload))
            await socket.send(json.dumps({"type": "playback_finished"}))
        elif kind == "dismissed":
            LOGGER.info("ignored (%s)", data.get("reason"))
        elif kind == "barge_in":
            LOGGER.info("interrupted — go ahead")
        elif kind == "conversation_end":
            LOGGER.info("conversation closed (%s)", data.get("reason"))
        elif kind == "error":
            LOGGER.error("%s: %s", data.get("code"), data.get("message"))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m clients.hands_free",
        description='Always-on "EVIE" listener (microphone → /v1/voice/live → speaker).',
    )
    parser.add_argument("--api-url", default=os.environ.get("EV_API_URL", "http://127.0.0.1:8000"))
    parser.add_argument(
        "--api-key",
        default=os.environ.get("EV_API_KEY") or os.environ.get("EV_MASTER_KEY", ""),
    )
    parser.add_argument("--device-id", default=os.environ.get("EV_DEVICE_ID", "mac-hands-free"))
    parser.add_argument("--device", default=None, help="input device name or index")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--frame-ms", type=int, default=20)
    parser.add_argument("--simulate-wav", default=None, help="use a WAV file instead of the mic")
    parser.add_argument("--no-audio", action="store_true", help="do not play reply audio")
    parser.add_argument("--duration", type=float, default=None, help="run for N seconds")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.list_devices:
        import sounddevice

        for index, device in enumerate(sounddevice.query_devices()):
            if device.get("max_input_channels", 0) > 0:
                print(f"[{index}] {device['name']} ({device['default_samplerate']:.0f} Hz)")
        return 0
    if not args.api_key:
        print(
            "ERROR: no API key. Pass --api-key or set EV_API_KEY / EV_MASTER_KEY.",
            file=sys.stderr,
        )
        return 2
    device: str | int | None = args.device
    if isinstance(device, str) and device.isdigit():
        device = int(device)
    config = ClientConfig(
        api_url=args.api_url,
        api_key=args.api_key,
        device_id=args.device_id,
        device=device,
        sample_rate=args.sample_rate,
        frame_ms=args.frame_ms,
        simulate_wav=args.simulate_wav,
        play_audio=not args.no_audio,
        duration_s=args.duration,
        log_level=args.log_level,
    )
    client = HandsFreeClient(config)

    async def run() -> int:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        code = await client.run(stop)
        LOGGER.info("stopped after %d wake(s), %d turn(s)", client.wakes, client.turns)
        return code

    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
