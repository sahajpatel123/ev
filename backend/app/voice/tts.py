"""Text-to-speech providers.

Production intent is Kokoro-82M INT8 ONNX on-device TTS (Apache-2.0, 54
voices) as the default real engine, with Chatterbox-Nano as an opt-in
expressive/cloned-voice tier. The dev ``meta`` provider renders prosody
metadata for offline tests; real engines degrade (``degraded=True``) when
weights or runtimes are missing rather than emitting fake audio.
"""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import struct
import sys
import tempfile
import wave
from pathlib import Path

import httpx

from app.compliance.policy import remote_processing_allowed
from app.config import settings
from app.ev.interaction import InteractionStrategy
from app.voice.contracts import (
    ModelUnavailableError,
    SpeechStyle,
    SynthesisResult,
    Synthesizer,
    VoiceError,
    acquire_model,
)


def speech_style_from_strategy(strategy: InteractionStrategy) -> SpeechStyle:
    """Map the intelligence-filter strategy to TTS controls.

    Urgency raises rate and lowers warmth padding; warmth softens delivery;
    brevity compresses length target into short prosody.
    """

    urgency = strategy.urgency
    warmth = 0.9 if strategy.emotional_state in ("excited", "sad") else 0.6
    brevity = 0.9 if strategy.mode in ("emergency", "casual") else 0.3
    return SpeechStyle(
        urgency=round(urgency, 3),
        warmth=round(warmth, 3),
        brevity=round(brevity, 3),
        mode=strategy.mode,
        length_target=strategy.length_target,
        directness=strategy.directness,
    )


def _speed_from_style(style: SpeechStyle) -> float:
    """Urgency/warmth/brevity -> speech rate (0.6-1.4)."""

    speed = 1.0 + 0.25 * style.urgency - 0.12 * style.warmth
    if style.brevity >= 0.6:
        speed += 0.08
    return round(max(0.6, min(1.4, speed)), 2)


def _wav_duration_ms(audio: bytes) -> int | None:
    """Real WAV duration, or None when the payload is not WAV."""

    try:
        with wave.open(io.BytesIO(audio), "rb") as wav:
            frames = wav.getnframes()
            rate = wav.getframerate()
    except (wave.Error, EOFError):
        return None
    return int(frames * 1000 / rate) if rate else None


def _concat_wav_chunks(chunks, sample_rate: int = 24000) -> bytes:
    """Concatenate float tensor chunks into one 16-bit mono WAV payload."""

    try:
        import numpy as np
    except ImportError:
        np = None  # type: ignore[assignment]

    samples: list[bytes] = []
    for chunk in chunks:
        if hasattr(chunk, "detach"):
            if np is None:
                raise RuntimeError(
                    "numpy is required to convert Kokoro/Chatterbox tensor output"
                )
            chunk = chunk.detach().cpu().numpy()
        if np is not None:
            values = np.asarray(chunk, dtype=np.float32).reshape(-1)
            pcm = np.clip(values * 32767.0, -32768.0, 32767.0).astype(np.int16)
            samples.append(pcm.tobytes())
        else:
            values = [max(-32768.0, min(32767.0, float(value) * 32767.0)) for value in chunk]
            samples.append(struct.pack(f"<{len(values)}h", *(int(round(v)) for v in values)))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"".join(samples))
    return buffer.getvalue()


class MetaSynthesizer:
    """Dev/test synthesizer: emits SSML-style prosody metadata, no audio."""

    name = "meta"

    async def synthesize(self, text: str, *, style: SpeechStyle) -> SynthesisResult:
        rate = 1.0 + 0.25 * style.urgency - 0.15 * style.warmth
        pitch = 1.0 + 0.08 * style.warmth
        volume = 0.8 + 0.2 * style.urgency
        ssml = (
            f'<speak><prosody rate="{rate:.2f}" pitch="{pitch:.2f}" volume="{volume:.2f}">'
            f"{text}</prosody></speak>"
        )
        return SynthesisResult(
            text=text,
            provider=self.name,
            content_type="text/plain",
            ssml=ssml,
            duration_ms=None,
            style=style,
            details={"engine": "dev-double"},
        )


def _tts_instructions(style: SpeechStyle) -> str:
    warmth = "warm and reassuring" if style.warmth >= 0.7 else "steady"
    pacing = "fast and clipped" if style.urgency >= 0.7 else "measured"
    brevity = "Keep the reply short and direct." if style.brevity >= 0.6 else ""
    return (
        f"Speak with a {warmth} tone at a {pacing} pace. "
        f"Match the {style.mode} register. {brevity}".strip()
    )


class OpenAICompatSynthesizer:
    """Natural TTS via any OpenAI-compatible /audio/speech endpoint."""

    name = "openai_compat"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        model: str = "gpt-4o-mini-tts",
        voice: str = "alloy",
        fmt: str = "mp3",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.fmt = fmt
        self._client = client

    async def synthesize(self, text: str, *, style: SpeechStyle) -> SynthesisResult:
        if not remote_processing_allowed("voice_tts"):
            raise RuntimeError(
                "Remote TTS is denied by regional policy; set EV_ALLOW_REMOTE_TTS=true"
            )
        speed = round(
            max(0.5, min(1.5, 0.95 + 0.30 * style.urgency - 0.15 * style.warmth)),
            2,
        )
        payload: dict = {
            "model": self.model,
            "voice": self.voice,
            "input": text,
            "response_format": self.fmt,
            "speed": speed,
        }
        if "tts" in self.model.lower():
            payload["instructions"] = _tts_instructions(style)
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        close = False
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=60)
            close = True
        try:
            resp = await client.post(
                f"{self.base_url}/audio/speech",
                headers=headers,
                json=payload,
            )
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                return SynthesisResult(
                    text=text,
                    provider=self.name,
                    style=style,
                    degraded=True,
                    details={
                        "reason": f"remote-http-{exc.response.status_code}",
                        "remote": True,
                    },
                )
        except httpx.HTTPError as exc:
            return SynthesisResult(
                text=text,
                provider=self.name,
                style=style,
                degraded=True,
                details={
                    "reason": f"{type(exc).__name__}",
                    "remote": True,
                },
            )
        finally:
            if close:
                await client.aclose()
        audio = resp.content
        content_type = f"audio/{self.fmt}"
        return SynthesisResult(
            text=text,
            provider=self.name,
            audio=audio,
            content_type=content_type,
            duration_ms=_wav_duration_ms(audio) if self.fmt == "wav" else None,
            style=style,
            details={"remote": True, "model": self.model},
        )


class PiperSynthesizer:
    """Legacy local neural TTS via the Piper CLI (ONNX voices).

    Maps urgency/warmth/brevity onto Piper prosody controls (length scale,
    noise scale, sentence silence). The subprocess runner is injectable for
    tests; without a model path the factory refuses rather than falling back.
    """

    name = "piper"

    def __init__(
        self,
        *,
        model: str | None = None,
        model_dir: str | None = None,
        binary: str | None = None,
        voice: str | None = None,
        length_scale: float | None = None,
        noise_scale: float | None = None,
        sentence_silence: float | None = None,
        runner=None,
    ) -> None:
        self.model = model or settings.voice_tts_model
        self.model_dir = model_dir or settings.voice_tts_model_dir
        self.binary = binary or settings.voice_tts_binary
        self.voice = voice if voice is not None else settings.voice_tts_voice
        self.length_scale = (
            length_scale if length_scale is not None else settings.voice_tts_length_scale
        )
        self.noise_scale = (
            noise_scale if noise_scale is not None else settings.voice_tts_noise_scale
        )
        self.sentence_silence = (
            sentence_silence
            if sentence_silence is not None
            else settings.voice_tts_sentence_silence
        )
        self._runner = runner

    def _model_path(self) -> str:
        if not self.model:
            raise RuntimeError("EV_VOICE_TTS_MODEL is required for piper (path to .onnx voice)")
        if self.model_dir and not os.path.isabs(self.model):
            return os.path.join(self.model_dir, self.model)
        return self.model

    def _style_args(self, style: SpeechStyle) -> list[str]:
        length_scale = self.length_scale * (1.0 - 0.15 * style.urgency + 0.05 * style.warmth)
        noise_scale = self.noise_scale * (1.0 - 0.08 * style.warmth + 0.12 * style.urgency)
        sentence_silence = self.sentence_silence * (1.0 - 0.3 * style.brevity)
        args = [
            "--length-scale",
            f"{max(0.1, min(2.0, length_scale)):.3f}",
            "--noise-scale",
            f"{max(0.1, min(2.0, noise_scale)):.3f}",
            "--sentence-silence",
            f"{max(0.0, min(2.0, sentence_silence)):.3f}",
        ]
        if self.voice and self.voice.strip().isdigit():
            args += ["--speaker", self.voice]
        return args

    async def _default_runner(
        self, argv: list[str], *, stdin: bytes
    ) -> tuple[int, bytes]:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate(stdin)
        return process.returncode or 0, stderr

    async def synthesize(self, text: str, *, style: SpeechStyle) -> SynthesisResult:
        model_path = self._model_path()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            output_path = handle.name
        argv = [
            self.binary,
            "--model",
            model_path,
            "--output_file",
            output_path,
            *self._style_args(style),
        ]
        runner = self._runner or self._default_runner
        try:
            returncode, stderr = await runner(argv, stdin=text.encode("utf-8"))
            if returncode != 0:
                message = stderr.decode("utf-8", errors="replace").strip()
                raise VoiceError(
                    f"Piper synthesis failed: {message or f'exit {returncode}'}",
                    status=502,
                    code="tts_engine_error",
                )
            with open(output_path, "rb") as audio_file:
                audio = audio_file.read()
            if not audio:
                raise VoiceError(
                    "Piper produced an empty audio file",
                    status=502,
                    code="tts_engine_error",
                )
        except OSError as exc:
            raise VoiceError(
                f"Piper synthesis failed: {type(exc).__name__}: {exc}",
                status=502,
                code="tts_engine_error",
            ) from exc
        finally:
            if os.path.exists(output_path):
                os.unlink(output_path)
        return SynthesisResult(
            text=text,
            provider=self.name,
            audio=audio,
            content_type="audio/wav",
            duration_ms=_wav_duration_ms(audio),
            style=style,
            details={"engine": self.name},
        )


# --------------------------------------------------------------------------- #
# Kokoro-82M INT8 ONNX — default real TTS engine
# --------------------------------------------------------------------------- #


class KokoroSynthesizer:
    """Kokoro-82M INT8 ONNX TTS (Apache-2.0, 54 voices) via the ``kokoro`` package.

    Loads through the ModelArbiter (on_demand slot, <=100 MB). Urgency/warmth/
    brevity map to speech rate. Missing package/weights produce a
    ``degraded=True`` result, never fake audio.
    """

    name = "kokoro-82m"

    def __init__(
        self,
        *,
        model_path: str | None = None,
        voices_path: str | None = None,
        voice: str | None = None,
        pipeline_factory=None,
    ) -> None:
        self.model_name = settings.voice_tts_engine
        self.model_path = model_path or _resolve_tts_path(self.model_name)
        self.voices_path = voices_path or _resolve_tts_voices_path(self.model_name)
        self.voice = voice or settings.voice_tts_kokoro_voice
        self._pipeline_factory = pipeline_factory
        self._pipeline = None

    @property
    def registry_name(self) -> str:
        return f"tts-{self.model_name}"

    def _load_pipeline(self):
        if self._pipeline is not None:
            return self._pipeline
        if self._pipeline_factory is not None:
            self._pipeline = self._pipeline_factory(self.model_path, self.voices_path)
            return self._pipeline
        if not self.model_path or not os.path.isfile(self.model_path):
            raise ModelUnavailableError(
                f"Kokoro ONNX weights not found at {self.model_path!r}; "
                "run `uv run python -m app.ml.cli pull tts-"
                f"{self.model_name}` (Agent 2 dependency)"
            )
        try:
            from kokoro import KPipeline
        except ImportError as exc:
            raise ModelUnavailableError(
                "the kokoro package is not installed; install it via Agent 2 "
                "dependency request"
            ) from exc
        kwargs = {"lang_code": "a", "model": self.model_path}
        if self.voices_path and os.path.isfile(self.voices_path):
            kwargs["voices"] = self.voices_path
        with acquire_model(self.registry_name):
            self._pipeline = KPipeline(**kwargs)
        return self._pipeline

    def _generate_sync(self, pipeline, text: str, speed: float):
        return list(pipeline(text, voice=self.voice, speed=speed))

    async def synthesize(self, text: str, *, style: SpeechStyle) -> SynthesisResult:
        try:
            pipeline = await asyncio.to_thread(self._load_pipeline)
        except ModelUnavailableError:
            return SynthesisResult(
                text=text,
                provider=self.name,
                style=style,
                degraded=True,
                details={"reason": "weights-or-runtime-unavailable"},
            )
        speed = _speed_from_style(style)
        try:
            chunks = await asyncio.to_thread(self._generate_sync, pipeline, text, speed)
            audio = _concat_wav_chunks([item[2] for item in chunks], sample_rate=24000)
        except Exception as exc:  # noqa: BLE001 - degrade on any engine failure
            return SynthesisResult(
                text=text,
                provider=self.name,
                style=style,
                degraded=True,
                details={"reason": f"{type(exc).__name__}: {exc}"},
            )
        if not audio:
            return SynthesisResult(
                text=text,
                provider=self.name,
                style=style,
                degraded=True,
                details={"reason": "empty synthesis output"},
            )
        return SynthesisResult(
            text=text,
            provider=self.name,
            audio=audio,
            content_type="audio/wav",
            duration_ms=_wav_duration_ms(audio),
            style=style,
            details={"engine": self.name, "voice": self.voice, "speed": speed},
        )


class ChatterboxSynthesizer:
    """Opt-in Chatterbox-Nano (110M, CPU, paralinguistic tags) expressive tier."""

    name = "chatterbox-nano"

    def __init__(
        self,
        *,
        model_name: str | None = None,
        speaker: str | None = None,
        model_factory=None,
    ) -> None:
        self.model_name = model_name or settings.voice_chatterbox_engine
        self.speaker = speaker or settings.voice_chatterbox_voice
        self._model_factory = model_factory
        self._model = None

    @property
    def registry_name(self) -> str:
        return f"tts-{self.model_name}"

    def _load_model(self):
        if self._model is not None:
            return self._model
        if self._model_factory is not None:
            self._model = self._model_factory(self.model_name)
            return self._model
        try:
            from chatterbox import Chatterbox
        except ImportError as exc:
            raise ModelUnavailableError(
                "the chatterbox package is not installed; install it via Agent 2 "
                "dependency request"
            ) from exc
        with acquire_model(self.registry_name):
            self._model = Chatterbox(model_name=self.model_name)
        return self._model

    def _generate_sync(self, model, text: str):
        result = model.generate(text, speaker=self.speaker)
        if isinstance(result, tuple):
            audio, sample_rate = result
            return _concat_wav_chunks([audio], sample_rate=int(sample_rate))
        return _concat_wav_chunks([result], sample_rate=16000)

    async def synthesize(self, text: str, *, style: SpeechStyle) -> SynthesisResult:
        try:
            model = await asyncio.to_thread(self._load_model)
        except ModelUnavailableError:
            return SynthesisResult(
                text=text,
                provider=self.name,
                style=style,
                degraded=True,
                details={"reason": "weights-or-runtime-unavailable"},
            )
        try:
            audio = await asyncio.to_thread(self._generate_sync, model, text)
        except Exception as exc:  # noqa: BLE001 - degrade on any engine failure
            return SynthesisResult(
                text=text,
                provider=self.name,
                style=style,
                degraded=True,
                details={"reason": f"{type(exc).__name__}: {exc}"},
            )
        if not audio:
            return SynthesisResult(
                text=text,
                provider=self.name,
                style=style,
                degraded=True,
                details={"reason": "empty synthesis output"},
            )
        return SynthesisResult(
            text=text,
            provider=self.name,
            audio=audio,
            content_type="audio/wav",
            duration_ms=_wav_duration_ms(audio),
            style=style,
            details={"engine": self.name, "speaker": self.speaker},
        )


# --------------------------------------------------------------------------- #
# Arbiter + factory
# --------------------------------------------------------------------------- #


def _resolve_tts_path(model_name: str) -> str | None:
    candidates = []
    if settings.voice_tts_model_dir:
        candidates.append(Path(settings.voice_tts_model_dir) / f"{model_name}.onnx")
    candidates.append(Path.home() / ".ev" / "models" / f"tts-{model_name}.onnx")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return str(candidates[-1])


def _resolve_tts_voices_path(model_name: str) -> str | None:
    candidates = []
    if settings.voice_tts_model_dir:
        candidates.append(Path(settings.voice_tts_model_dir) / f"{model_name}.voices.bin")
    candidates.append(Path.home() / ".ev" / "models" / f"tts-{model_name}.voices.bin")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


DEFAULT_PIPER_VOICE = "en_US-lessac-medium"


def piper_voice_path() -> str | None:
    """Installed Piper voice (.onnx), preferring an explicit configuration."""

    candidates: list[Path] = []
    configured = settings.voice_tts_model
    if configured and configured.endswith(".onnx"):
        base = Path(configured)
        if settings.voice_tts_model_dir and not base.is_absolute():
            base = Path(settings.voice_tts_model_dir) / base
        candidates.append(base)
    if settings.voice_tts_model_dir:
        candidates.append(Path(settings.voice_tts_model_dir) / f"{DEFAULT_PIPER_VOICE}.onnx")
    candidates.append(Path.home() / ".ev" / "models" / f"{DEFAULT_PIPER_VOICE}.onnx")
    for candidate in candidates:
        expanded = candidate.expanduser()
        if expanded.is_file():
            return str(expanded)
    return None


def piper_binary_path() -> str | None:
    """Piper CLI on PATH, or the console script inside the active venv.

    launchd and other supervisors start EVIE with a minimal PATH that does not
    include ``.venv/bin``, so an installed Piper must still be found.
    """

    found = shutil.which(settings.voice_tts_binary)
    if found:
        return found
    candidate = Path(sys.prefix) / "bin" / settings.voice_tts_binary
    return str(candidate) if candidate.is_file() else None


def real_tts_available() -> bool:
    """True when EVIE can produce actual reply audio server-side."""

    if settings.voice_tts_provider == "openai_compat":
        return bool(settings.voice_tts_base_url)
    return bool(piper_voice_path() and piper_binary_path())


def get_synthesizer() -> Synthesizer:
    provider = settings.voice_tts_provider
    if provider == "auto":
        voice = piper_voice_path()
        binary = piper_binary_path()
        if voice and binary:
            return PiperSynthesizer(model=voice, binary=binary)
        if os.path.isfile(_resolve_tts_path(settings.voice_tts_engine) or ""):
            return KokoroSynthesizer()
        return MetaSynthesizer()
    if provider == "openai_compat":
        if not settings.voice_tts_base_url:
            raise RuntimeError("EV_VOICE_TTS_BASE_URL is required for openai_compat TTS")
        if not remote_processing_allowed("voice_tts"):
            raise RuntimeError(
                "Remote TTS is denied by regional policy; set EV_ALLOW_REMOTE_TTS=true"
            )
        return OpenAICompatSynthesizer(
            base_url=settings.voice_tts_base_url,
            api_key=settings.voice_tts_api_key,
            model=settings.voice_tts_model,
            voice=settings.voice_tts_voice,
            fmt=settings.voice_tts_format,
        )
    if provider == "piper":
        if not settings.voice_tts_model:
            raise RuntimeError("EV_VOICE_TTS_MODEL is required for piper (path to .onnx voice)")
        return PiperSynthesizer()
    if provider == "kokoro":
        return KokoroSynthesizer()
    if provider == "chatterbox":
        return ChatterboxSynthesizer()
    if provider == "meta":
        return MetaSynthesizer()
    raise RuntimeError(f"unknown voice_tts_provider {provider!r}")
