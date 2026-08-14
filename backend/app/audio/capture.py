"""Microphone capture for the always-on ears process.

The capture layer targets 16 kHz mono int16 PCM — the contract shared by the
VAD, wake, and scene models. It uses sounddevice (PortAudio) when installed,
with an injectable module so offline CI exercises the same code paths with a
fake stream.

Permission handling is deliberately loud: a denied macOS Microphone (TCC)
permission surfaces as ``MicrophoneDeniedError`` with exact remediation steps
instead of a silent zero-audio stream.
"""

from __future__ import annotations

import array
import wave
from dataclasses import dataclass
from io import BytesIO
from typing import Any


class MicrophoneUnavailableError(RuntimeError):
    """sounddevice/PortAudio is not installed or no input device exists."""


class MicrophoneDeniedError(RuntimeError):
    """The OS refused microphone access (macOS TCC or equivalent)."""


def _tcc_hint(error_text: str) -> str:
    return (
        "Microphone permission appears to be denied. On macOS enable it in "
        "System Settings > Privacy & Security > Microphone, then restart the "
        f"ears process. (PortAudio: {error_text})"
    )


def _import_sounddevice():
    try:
        import sounddevice as sd
    except ImportError as exc:
        raise MicrophoneUnavailableError(
            "sounddevice is not installed. The ears process needs it for "
            "real microphone capture; add it as an Agent 2 dependency "
            "(pip package: sounddevice, wheels are small and statically "
            "bundled with PortAudio)."
        ) from exc
    return sd


def _is_permission_error(exc: BaseException) -> bool:
    text = " ".join(str(arg) for arg in getattr(exc, "args", ())).lower()
    markers = (
        "device unavailable",
        "permission",
        "denied",
        "not permitted",
        "microphone",
        "tcc",
        "auth",
        "error -38",
    )
    return any(marker in text for marker in markers)


def probe_input_rms(
    device: str | int | None,
    *,
    seconds: float = 0.35,
    sounddevice_module: Any | None = None,
) -> float:
    """Record a short clip and return RMS. 0.0 means silent or unusable."""

    try:
        sd = sounddevice_module or _import_sounddevice()
    except (MicrophoneUnavailableError, MicrophoneDeniedError):
        return 0.0
    try:
        info = sd.query_devices(device)
        rate = int(info.get("default_samplerate") or 16000)
        frames = max(1, int(rate * max(0.15, seconds)))
        audio = sd.rec(
            frames,
            samplerate=rate,
            channels=1,
            dtype="int16",
            device=device,
        )
        sd.wait()
        if audio is None:
            return 0.0
        flat = audio.reshape(-1)
        if getattr(flat, "size", len(flat)) == 0:
            return 0.0
        total = sum(int(sample) * int(sample) for sample in flat)
        return (total / len(flat)) ** 0.5
    except Exception:
        return 0.0


def list_input_devices(sounddevice_module: Any | None = None) -> list[dict]:
    """List PortAudio input devices as plain dicts (no raw audio)."""

    try:
        sd = sounddevice_module or _import_sounddevice()
    except (MicrophoneUnavailableError, MicrophoneDeniedError):
        raise
    except ImportError as exc:
        raise MicrophoneUnavailableError(
            "sounddevice is not installed; install it to use real microphone capture"
        ) from exc
    try:
        devices = sd.query_devices()
    except Exception as exc:  # PortAudio init/query failures
        if _is_permission_error(exc):
            raise MicrophoneDeniedError(_tcc_hint(str(exc))) from exc
        raise MicrophoneUnavailableError(f"Could not query audio devices: {exc}") from exc
    result: list[dict] = []
    for index, device in enumerate(devices):
        if device.get("max_input_channels", 0) > 0:
            result.append(
                {
                    "index": index,
                    "name": device.get("name"),
                    "default_samplerate": device.get("default_samplerate"),
                    "max_input_channels": device.get("max_input_channels"),
                }
            )
    return result


def _resolve_device(sd: Any, device: str | None) -> Any:
    if device is None:
        return None
    if device.isdigit():
        return int(device)
    try:
        return int(sd.query_devices(device))
    except Exception:
        return device  # PortAudio accepts names too


@dataclass
class CaptureBlock:
    """One capture callback payload: mono PCM16 samples + absolute sample index."""

    samples: array.array
    first_sample_index: int


class MicrophoneStream:
    """16 kHz mono int16 InputStream wrapper.

    The PortAudio callback appends into a user-supplied ring (or an internal
    one); the reader consumes from the ring, so a slow consumer never blocks
    the audio thread.
    """

    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        block_ms: int = 20,
        device: str | None = None,
        ring=None,
        sounddevice_module: Any | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.block_ms = block_ms
        self.device = device
        self._sd = sounddevice_module
        self._stream: Any | None = None
        self._ring = ring
        self._sample_count = 0
        self._input_sample_rate = sample_rate

    @property
    def ring(self):
        if self._ring is None:
            from app.audio.ring import PCM16RingBuffer

            self._ring = PCM16RingBuffer(self.sample_rate * 10)
        return self._ring

    def _callback(self, indata, frames: int, time_info, status) -> None:
        # PortAudio int16 input is shape (frames, channels); force mono.
        if indata.ndim > 1:
            indata = indata[:, 0]
        samples = array.array("h", indata.astype("<i2").reshape(-1).tolist())
        if self._input_sample_rate != self.sample_rate:
            samples = self._resample(samples, self._input_sample_rate, self.sample_rate)
        self.ring.write(samples)
        self._sample_count += len(samples)

    @staticmethod
    def _resample(
        samples: array.array,
        source_rate: int,
        target_rate: int,
    ) -> array.array:
        """Linear-resample one short mono block without a heavy audio dependency."""

        if source_rate <= 0 or target_rate <= 0 or source_rate == target_rate:
            return samples
        if len(samples) < 2:
            return array.array("h", samples)
        target_length = max(1, round(len(samples) * target_rate / source_rate))
        result = array.array("h")
        scale = (len(samples) - 1) / max(1, target_length - 1)
        for index in range(target_length):
            position = index * scale
            left = int(position)
            right = min(left + 1, len(samples) - 1)
            fraction = position - left
            value = samples[left] + (samples[right] - samples[left]) * fraction
            result.append(max(-32768, min(32767, round(value))))
        return result

    def open(self) -> None:
        if self._stream is not None:
            return
        try:
            sd = self._sd or _import_sounddevice()
        except (MicrophoneUnavailableError, MicrophoneDeniedError):
            raise
        except ImportError as exc:
            raise MicrophoneUnavailableError(
                "sounddevice is not installed; install it to use real microphone capture"
            ) from exc
        device = _resolve_device(sd, self.device)
        input_rate = self.sample_rate
        try:
            info = sd.query_devices(device)
            if isinstance(info, dict):
                candidate = int(float(info.get("default_samplerate") or 0))
                if candidate >= 8000:
                    input_rate = candidate
        except Exception:
            # Some test doubles and PortAudio hosts do not expose a per-device
            # query. The requested rate remains the safe fallback.
            pass
        self._input_sample_rate = input_rate
        blocksize = max(1, int(input_rate * self.block_ms / 1000))
        try:
            self._stream = sd.InputStream(
                samplerate=input_rate,
                blocksize=blocksize,
                device=device,
                channels=1,
                dtype="int16",
                callback=self._callback,
            )
        except Exception as exc:
            if _is_permission_error(exc):
                raise MicrophoneDeniedError(_tcc_hint(str(exc))) from exc
            raise MicrophoneUnavailableError(
                f"Could not open microphone input at {self.sample_rate} Hz: {exc}"
            ) from exc
        try:
            self._stream.start()
        except Exception as exc:
            if _is_permission_error(exc):
                raise MicrophoneDeniedError(_tcc_hint(str(exc))) from exc
            raise MicrophoneUnavailableError(f"Could not start microphone stream: {exc}") from exc

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
            finally:
                try:
                    self._stream.close()
                finally:
                    self._stream = None

    def __enter__(self) -> MicrophoneStream:
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def pcm_to_wav_bytes(samples, sample_rate: int = 16000) -> bytes:
    """Package mono PCM16 samples into an in-memory WAV (never touches disk)."""

    if isinstance(samples, (bytes, bytearray, memoryview)):
        frames = bytes(samples)
    else:
        frames = array.array("h", samples).tobytes()
    buffer = BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(frames)
    return buffer.getvalue()


def wav_pcm16_samples(data: bytes) -> tuple[array.array, int]:
    """Read mono PCM16 samples + rate from WAV bytes (used by VAD/scene paths)."""

    with wave.open(BytesIO(data), "rb") as wav:
        if wav.getsampwidth() != 2:
            raise ValueError("ears audio must be 16-bit PCM WAV")
        channels = wav.getnchannels()
        rate = wav.getframerate()
        raw = wav.readframes(wav.getnframes())
    samples = array.array("h", raw)
    if channels > 1:
        mono = array.array("h")
        for i in range(0, len(samples) - channels + 1, channels):
            mono.append(sum(samples[i : i + channels]) // channels)
        samples = mono
    return samples, rate


def samples_to_bytes(samples) -> bytes:
    """Serialize an iterable of ints into little-endian PCM16 bytes."""

    if isinstance(samples, (bytes, bytearray, memoryview)):
        return bytes(samples)
    return array.array("h", samples).tobytes()
