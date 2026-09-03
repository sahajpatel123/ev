"""Speech-to-text providers.

Production intent is Parakeet-EOU-120M INT8 ONNX on-device streaming ASR with
inline end-of-utterance detection. The dev ``echo`` provider exists only for
offline tests; real providers fail closed when audio is missing or
undecodable, and degrade (``degraded=True``, ``confidence=0.0``) when weights
are absent instead of fabricating a transcript or echoing the caller's hint.
"""

from __future__ import annotations

import array
import asyncio
import base64
import io
import math
import os
import tempfile
import time
import wave
from collections.abc import AsyncIterator
from pathlib import Path

import httpx

from app.compliance.policy import remote_processing_allowed
from app.config import settings
from app.voice.contracts import (
    ModelUnavailableError,
    Transcriber,
    Transcript,
    TranscriptPartial,
    VoiceError,
    acquire_model,
)

# --------------------------------------------------------------------------- #
# Hear classification: never a silent drop
# --------------------------------------------------------------------------- #


HEAR_CODES = frozenset(
    {
        "asr_no_speech",
        "asr_empty_result",
        "asr_degraded",
        "asr_undecodable_audio",
        "asr_empty_audio",
        "asr_bad_base64",
        "asr_audio_required",
        "mic_denied",
        "asr_device_unusable",
    }
)


def classify_hear_failure(
    *,
    code: str | None = None,
    empty_audio: bool = False,
    undecodable: bool = False,
    no_speech: bool = False,
    empty_result: bool = False,
    degraded: bool = False,
    mic_denied: bool = False,
    device_unusable: bool = False,
) -> tuple[str, str]:
    """Map a hear failure to a typed owner-visible (code, message)."""

    if code and code in HEAR_CODES:
        return code, hear_status_message(code)
    if mic_denied:
        return "mic_denied", hear_status_message("mic_denied")
    if device_unusable:
        return "asr_device_unusable", hear_status_message("asr_device_unusable")
    if empty_audio:
        return "asr_empty_audio", hear_status_message("asr_empty_audio")
    if undecodable:
        return "asr_undecodable_audio", hear_status_message("asr_undecodable_audio")
    if degraded:
        return "asr_degraded", hear_status_message("asr_degraded")
    if no_speech:
        return "asr_no_speech", hear_status_message("asr_no_speech")
    if empty_result:
        return "asr_empty_result", hear_status_message("asr_empty_result")
    return "asr_empty_result", hear_status_message("asr_empty_result")


def hear_failure_from_exception(exc: BaseException) -> tuple[str, str]:
    """Map a capture/OS error to a typed hear failure (mic denied, unusable)."""

    text = f"{type(exc).__name__} {exc}".lower()
    if "denied" in text or "tcc" in text or "not permitted" in text:
        return classify_hear_failure(mic_denied=True)
    if "unavailable" in text or "no input" in text or "device" in text:
        return classify_hear_failure(device_unusable=True)
    return classify_hear_failure(device_unusable=True)


def hear_status_message(code: str) -> str:
    """Owner-facing line for a typed hear failure. Never an empty string."""

    return {
        "asr_no_speech": "I didn't hear any speech in that clip.",
        "asr_empty_result": "I didn't catch that. Hold Push to talk and try again.",
        "asr_degraded": "Speech recognition is unavailable. Check the ASR engine or weights.",
        "asr_undecodable_audio": "I couldn't read that clip. Hold Push to talk and try again.",
        "asr_empty_audio": "I couldn't read that clip. Hold Push to talk and try again.",
        "asr_bad_base64": "I couldn't read that clip. Hold Push to talk and try again.",
        "asr_audio_required": "I couldn't read that clip. Hold Push to talk and try again.",
        "mic_denied": (
            "Microphone permission is denied. Enable it in System Settings → "
            "Privacy & Security → Microphone, then try again."
        ),
        "asr_device_unusable": (
            "The microphone device is unusable. Pick another input and try again."
        ),
        "asr_timeout": "That took too long to hear. Try a shorter question.",
        "asr_unreadable": "I didn't catch that. Hold Push to talk and try again.",
    }.get(code, "I didn't catch that. Hold Push to talk and try again.")


def pcm_rms(pcm: bytes) -> float:
    """RMS of little-endian 16-bit PCM. 0.0 for empty or odd-length buffers."""

    if len(pcm) < 4:
        return 0.0
    samples = array.array("h")
    try:
        samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    except Exception:
        return 0.0
    if not samples:
        return 0.0
    acc = 0.0
    for sample in samples:
        acc += float(sample) * float(sample)
    return math.sqrt(acc / len(samples))


def wav_is_silent(raw: bytes, *, rms_floor: float = 180.0) -> bool:
    """True when a WAV/PCM buffer is empty or below the speech-energy floor."""

    try:
        normalized = normalize_asr_audio(raw)
        with wave.open(io.BytesIO(normalized), "rb") as wav:
            pcm = wav.readframes(wav.getnframes())
    except VoiceError:
        return True
    except Exception:
        return True
    return pcm_rms(pcm) < rms_floor


def normalize_asr_audio(raw: bytes) -> bytes:
    """Wrap PCM, convert containers, resample to 16 kHz mono 16-bit WAV.

    Phone/Mac clips often arrive as raw PCM16, 44.1/48 kHz WAV, or m4a.
    Every shipped transcriber must see the same 16 kHz contract — never
    reject a readable clip as ``asr_undecodable_audio`` just for rate.
    """

    if not raw:
        raise VoiceError(
            hear_status_message("asr_empty_audio"),
            status=422,
            code="asr_empty_audio",
        )
    try:
        from app.audio.capture import pcm_to_wav_bytes
        from app.voice.speaker import decode_waveform, ensure_wav_bytes

        wav = ensure_wav_bytes(raw)
        values, rate = decode_waveform(wav)
        if not values:
            raise VoiceError(
                hear_status_message("asr_empty_audio"),
                status=422,
                code="asr_empty_audio",
            )
        pcm = array.array(
            "h",
            (max(-32768, min(32767, int(round(sample * 32767)))) for sample in values),
        )
        return pcm_to_wav_bytes(pcm.tobytes(), 16000 if rate else 16000)
    except VoiceError:
        raise
    except ValueError as exc:
        raise VoiceError(
            hear_status_message("asr_undecodable_audio"),
            status=422,
            code="asr_undecodable_audio",
        ) from exc
    except Exception as exc:
        raise VoiceError(
            hear_status_message("asr_undecodable_audio"),
            status=422,
            code="asr_undecodable_audio",
        ) from exc


# --------------------------------------------------------------------------- #
# Audio input: fail closed, allowlisted refs only
# --------------------------------------------------------------------------- #


def clip_wav_to_max_seconds(raw: bytes, max_seconds: float | None = None) -> bytes:
    """Keep the last ``max_seconds`` of a PCM WAV so ASR cannot run away.

    A mislabeled 48 kHz float capture wrapped as 16 kHz int16 looks like a
    multi-minute clip. Push-to-talk then sits in Whisper until the client
    times out. Bounding duration is the server-side backstop; the Mac client
    also converts to real 16 kHz PCM16 before upload.
    """

    limit = float(
        max_seconds if max_seconds is not None else settings.voice_utterance_max_seconds
    )
    if limit <= 0 or not raw.startswith(b"RIFF"):
        return raw
    try:
        with wave.open(io.BytesIO(raw), "rb") as src:
            rate = src.getframerate()
            channels = src.getnchannels()
            width = src.getsampwidth()
            nframes = src.getnframes()
            if rate <= 0 or nframes <= 0:
                return raw
            max_frames = int(rate * limit)
            if nframes <= max_frames:
                return raw
            src.setpos(nframes - max_frames)
            frames = src.readframes(max_frames)
    except (wave.Error, EOFError):
        return raw
    out = io.BytesIO()
    with wave.open(out, "wb") as dst:
        dst.setnchannels(channels)
        dst.setsampwidth(width)
        dst.setframerate(rate)
        dst.writeframes(frames)
    return out.getvalue()


def _object_store_key(ref: str) -> str | None:
    """Return the object-store key for an ``ev://`` ref, else None."""

    if ref.startswith("ev://"):
        return ref[len("ev://") :].lstrip("/")
    return None


def _allowed_audio_roots() -> list[Path]:
    roots = [Path(settings.storage_root) / "voice"]
    for raw in settings.voice_asr_allowed_roots or []:
        if raw:
            roots.append(Path(raw))
    roots.append(Path(tempfile.gettempdir()))
    return [root.expanduser().resolve() for root in roots]


def _safe_local_path(ref: str) -> Path:
    """Resolve an audio_ref path and reject anything outside the allowlist."""

    path = Path(ref).expanduser().resolve()
    for root in _allowed_audio_roots():
        try:
            path.relative_to(root)
        except ValueError:
            continue
        return path
    raise VoiceError(
        "audio_ref is outside the allowlisted voice audio roots",
        status=403,
        code="asr_audio_ref_denied",
    )


async def _read_audio(audio_b64: str | None, audio_ref: str | None) -> tuple[bytes, str]:
    """Read audio bytes from base64 or an allowlisted ref. Never echo a hint."""

    if audio_b64 is not None:
        try:
            raw = base64.b64decode(audio_b64, validate=True)
        except Exception as exc:
            raise VoiceError(
                "audio_b64 must be valid base64",
                status=422,
                code="asr_bad_base64",
            ) from exc
        if not raw:
            raise VoiceError(
                hear_status_message("asr_empty_audio"),
                status=422,
                code="asr_empty_audio",
            )
        return clip_wav_to_max_seconds(normalize_asr_audio(raw)), "voice.wav"
    if audio_ref:
        key = _object_store_key(audio_ref)
        if key is not None:
            if not key.startswith("voice/"):
                raise VoiceError(
                    "audio_ref object keys must live under voice/",
                    status=403,
                    code="asr_audio_ref_denied",
                )
            from app.storage.object_store import get_object_store

            try:
                raw = await get_object_store().get(key)
            except Exception as exc:
                raise VoiceError(
                    "audio_ref object not found",
                    status=404,
                    code="asr_audio_ref_missing",
                ) from exc
            return clip_wav_to_max_seconds(normalize_asr_audio(raw)), Path(key).name
        path = _safe_local_path(audio_ref)
        if not path.is_file():
            raise VoiceError(
                "audio_ref must be a readable local audio file",
                status=404,
                code="asr_audio_ref_missing",
            )
        return clip_wav_to_max_seconds(normalize_asr_audio(path.read_bytes())), path.name
    raise VoiceError(
        "ASR requires audio_b64 or a readable audio_ref; real engines never "
        "accept a transcript hint in place of audio",
        status=422,
        code="asr_audio_required",
    )


def _wav_pcm(data: bytes) -> tuple[array.array, int]:
    """Decode 16-bit PCM WAV to mono 16 kHz samples."""

    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            channels = wav.getnchannels()
            width = wav.getsampwidth()
            rate = wav.getframerate()
            frames = wav.readframes(wav.getnframes())
    except (wave.Error, EOFError) as exc:
        raise VoiceError(
            "audio must be a readable WAV file",
            status=422,
            code="asr_undecodable_audio",
        ) from exc
    if width not in (1, 2):
        raise VoiceError(
            hear_status_message("asr_undecodable_audio"),
            status=422,
            code="asr_undecodable_audio",
        )
    if width == 1:
        samples = array.array("h", ((byte - 128) * 256 for byte in frames))
    else:
        samples = array.array("h")
        samples.frombytes(frames)
    if channels > 1:
        samples = array.array(
            "h",
            (
                sum(samples[i : i + channels]) // channels
                for i in range(0, len(samples) - channels + 1, channels)
            ),
        )
    if rate != 16000:
        from app.voice.speaker import _resample

        floated = [sample / 32768.0 for sample in samples]
        resampled = _resample(floated, rate, 16000)
        samples = array.array(
            "h",
            (max(-32768, min(32767, int(round(value * 32767)))) for value in resampled),
        )
        rate = 16000
    return samples, rate


def _wav_bytes(pcm: array.array, sample_rate: int = 16000) -> bytes:
    """Wrap PCM samples back into a 16-bit mono WAV payload."""

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm.tobytes())
    return buffer.getvalue()


async def _stream_final(
    transcriber: Transcriber,
    *,
    audio_ref: str | None = None,
    audio_b64: str | None = None,
    text_hint: str | None = None,
    language: str = "en",
) -> AsyncIterator[Transcript | TranscriptPartial]:
    yield await transcriber.transcribe(
        audio_ref=audio_ref,
        audio_b64=audio_b64,
        text_hint=text_hint,
        language=language,
    )


# --------------------------------------------------------------------------- #
# Dev/test double
# --------------------------------------------------------------------------- #


class EchoTranscriber:
    """Offline dev/test transcriber.

    Returns the supplied hint verbatim with confidence 0.0 — it is a test
    double, never a transcription, and refuses audio it cannot understand.
    """

    name = "echo"

    async def transcribe(
        self,
        *,
        audio_ref: str | None = None,
        audio_b64: str | None = None,
        text_hint: str | None = None,
        language: str = "en",
    ) -> Transcript:
        if audio_b64 is not None or audio_ref is not None:
            raise VoiceError(
                "echo/dev ASR cannot transcribe audio; provide a text hint or "
                "configure a real engine (EV_VOICE_ASR_PROVIDER=parakeet)",
                status=422,
                code="asr_echo_no_audio",
            )
        if text_hint is None or not text_hint.strip():
            raise VoiceError(
                "ASR requires a text hint in echo/dev mode",
                status=422,
                code="asr_audio_required",
            )
        return Transcript(
            text=text_hint.strip(),
            confidence=0.0,
            language=language,
            provider=self.name,
            degraded=False,
            details={"engine": "dev-double"},
        )

    def stream(
        self,
        *,
        audio_ref: str | None = None,
        audio_b64: str | None = None,
        text_hint: str | None = None,
        language: str = "en",
    ) -> AsyncIterator[Transcript | TranscriptPartial]:
        return _stream_final(
            self,
            audio_ref=audio_ref,
            audio_b64=audio_b64,
            text_hint=text_hint,
            language=language,
        )


# --------------------------------------------------------------------------- #
# Remote OpenAI-compatible ASR
# --------------------------------------------------------------------------- #


class OpenAICompatTranscriber:
    """Whisper-class ASR via any OpenAI-compatible /audio/transcriptions endpoint."""

    name = "openai_compat"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        model: str = "whisper-1",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._client = client

    async def transcribe(
        self,
        *,
        audio_ref: str | None = None,
        audio_b64: str | None = None,
        text_hint: str | None = None,
        language: str = "en",
    ) -> Transcript:
        audio, filename = await _read_audio(audio_b64, audio_ref)
        content_type = {
            ".wav": "audio/wav",
            ".mp3": "audio/mpeg",
            ".ogg": "audio/ogg",
            ".m4a": "audio/mp4",
            ".flac": "audio/flac",
        }.get(Path(filename).suffix.lower(), "audio/wav")

        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        close = False
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=30)
            close = True
        try:
            resp = await client.post(
                f"{self.base_url}/audio/transcriptions",
                headers=headers,
                data={"model": self.model, "language": language},
                files={"file": (filename, audio, content_type)},
            )
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                return Transcript(
                    text="",
                    confidence=0.0,
                    language=language,
                    provider=self.name,
                    degraded=True,
                    audio_ref=audio_ref,
                    details={
                        "reason": f"remote-http-{exc.response.status_code}",
                        "remote": True,
                    },
                )
        except httpx.HTTPError as exc:
            return Transcript(
                text="",
                confidence=0.0,
                language=language,
                provider=self.name,
                degraded=True,
                audio_ref=audio_ref,
                details={
                    "reason": type(exc).__name__,
                    "remote": True,
                },
            )
        finally:
            if close:
                await client.aclose()
        data = resp.json()
        text = (data.get("text") or "").strip()
        if not text:
            raise VoiceError(
                "ASR returned an empty transcript",
                status=502,
                code="asr_empty_result",
            )
        raw_confidence = data.get("confidence")
        # Only trust a provider-supplied confidence; unknown means 0.0.
        confidence = (
            float(raw_confidence)
            if isinstance(raw_confidence, (int, float))
            else 0.0
        )
        return Transcript(
            text=text,
            confidence=round(min(1.0, max(0.0, confidence)), 4),
            language=language,
            provider=self.name,
            audio_ref=audio_ref,
            details={"remote": True},
        )

    def stream(
        self,
        *,
        audio_ref: str | None = None,
        audio_b64: str | None = None,
        text_hint: str | None = None,
        language: str = "en",
    ) -> AsyncIterator[Transcript | TranscriptPartial]:
        return _stream_final(
            self,
            audio_ref=audio_ref,
            audio_b64=audio_b64,
            text_hint=text_hint,
            language=language,
        )


# --------------------------------------------------------------------------- #
# Legacy local Whisper (faster-whisper) — kept as an opt-in provider
# --------------------------------------------------------------------------- #

# Process-wide CTranslate2 weights. VoiceRuntime is constructed per request;
# without this, every VAD segment reloads faster-whisper-base (~150 MB).
_WHISPER_MODELS: dict[tuple, object] = {}


class FasterWhisperTranscriber:
    """Local Whisper-class ASR via faster-whisper (CTranslate2).

    Accepts ``audio_b64`` or an allowlisted ``audio_ref``, honors a
    per-call/configured language, and runs transcription in a worker thread.
    The faster-whisper import is lazy and the model factory is injectable for
    tests. ``vad_filter`` is wired to ``EV_VOICE_ASR_VAD_FILTER``.
    """

    name = "faster_whisper"

    def __init__(
        self,
        *,
        model: str | None = None,
        model_dir: str | None = None,
        device: str | None = None,
        compute_type: str | None = None,
        language: str | None = None,
        vad_filter: bool | None = None,
        model_factory=None,
    ) -> None:
        self.model_name = model or settings.voice_asr_model
        if self.model_name in {"whisper-1", "gpt-4o-mini-transcribe", None, ""}:
            self.model_name = "base"
        self.model_dir = model_dir or settings.voice_asr_model_dir
        self.device = device or settings.voice_asr_device
        self.compute_type = compute_type or settings.voice_asr_compute_type
        self.language = language if language is not None else settings.voice_asr_language
        self.vad_filter = (
            settings.voice_asr_vad_filter if vad_filter is None else vad_filter
        )
        self.wake_no_speech_threshold = settings.voice_asr_wake_no_speech_threshold
        self._model_factory = model_factory
        self._model = None

    def _load_model(self):
        if self._model is not None:
            return self._model
        if self._model_factory is not None:
            self._model = self._model_factory(
                self.model_name,
                device=self.device,
                compute_type=self.compute_type,
                download_root=self.model_dir,
            )
            return self._model
        key = (self.model_name, self.device, self.compute_type, self.model_dir)
        cached = _WHISPER_MODELS.get(key)
        if cached is not None:
            self._model = cached
            return cached
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise ModelUnavailableError(
                "faster-whisper is not installed; install the Agent 2 dependency"
            ) from exc
        load_kwargs = {
            "device": self.device,
            "compute_type": self.compute_type,
            "download_root": self.model_dir,
        }
        try:
            # Cached weights: skip the Hugging Face HEAD that ran on every
            # ev.ears restart and spiked the MacBook after EV.app quit.
            self._model = WhisperModel(
                self.model_name, local_files_only=True, **load_kwargs
            )
        except Exception:
            self._model = WhisperModel(self.model_name, **load_kwargs)
        _WHISPER_MODELS[key] = self._model
        return self._model

    async def _resolve_audio(self, audio_b64: str | None, audio_ref: str | None) -> str:
        audio, _filename = await _read_audio(audio_b64, audio_ref)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            handle.write(audio)
            return handle.name

    def _transcribe_sync(self, path: str, language: str, *, wake_mode: bool = False) -> Transcript:
        model = self._load_model()
        kwargs: dict = {
            "language": language,
            # Talk clips are already user-cut (≤15s). Silero VAD on a short
            # hold often deletes the whole buffer and the Mac app then shows
            # a red EVAPIError instead of a reply.
            "vad_filter": False,
        }
        if wake_mode:
            kwargs.update(
                condition_on_previous_text=False,
                # 0.1 suppressed real short wake clips (measured: the owner's
                # "EVIE" came back empty on 2.5-6 s takes). Whisper's own
                # default (0.6) keeps real speech; the wake engine gates weak
                # aliases ("Eve"/"evil") on the per-segment no_speech_prob
                # below instead of on this suppression threshold.
                no_speech_threshold=self.wake_no_speech_threshold,
                beam_size=1,
                temperature=0.0,
                initial_prompt="EVIE. Hey EVIE.",
            )
        else:
            # A single beam is enough for already-cut Talk clips and is
            # what keeps ASR inside a human reply interval.
            kwargs.update(
                condition_on_previous_text=False,
                beam_size=1,
                temperature=0.0,
            )
        try:
            segments, info = model.transcribe(path, **kwargs)
        except TypeError:
            kwargs.pop("condition_on_previous_text", None)
            kwargs.pop("beam_size", None)
            kwargs.pop("temperature", None)
            kwargs.pop("initial_prompt", None)
            kwargs.pop("no_speech_threshold", None)
            segments, info = model.transcribe(path, **kwargs)
        seg_list = list(segments)
        text = "".join(segment.text for segment in seg_list).strip()
        # Best speech evidence across segments: min no_speech_prob (Whisper's
        # own hallucination signal, 0..1, >0.6 ≈ silence). Real "EVIE" clips
        # score ~0.2-0.4; silence hallucinations score ~0.8+.
        no_speech_prob: float | None = None
        avg_logprob = getattr(info, "avg_logprob", None)
        for segment in seg_list:
            value = getattr(segment, "no_speech_prob", None)
            if value is not None:
                no_speech_prob = (
                    min(no_speech_prob, float(value))
                    if no_speech_prob is not None
                    else float(value)
                )
            if avg_logprob is None:
                segment_avg = getattr(segment, "avg_logprob", None)
                if segment_avg is not None:
                    avg_logprob = segment_avg
        details = {
            "engine": "faster-whisper",
            "no_speech_prob": no_speech_prob,
            "avg_logprob": float(avg_logprob) if avg_logprob is not None else None,
        }
        if not text:
            if wake_mode:
                details["reason"] = "empty"
                return Transcript(
                    text="",
                    confidence=0.0,
                    language=language,
                    provider=self.name,
                    details=details,
                )
            raise VoiceError(
                "ASR returned an empty transcript",
                status=502,
                code="asr_empty_result",
            )
        confidence = (
            math.exp(max(-10.0, float(avg_logprob))) if avg_logprob is not None else 0.0
        )
        duration = getattr(info, "duration", None)
        return Transcript(
            text=text,
            confidence=round(min(1.0, confidence), 4),
            language=language,
            provider=self.name,
            duration_ms=int(duration * 1000) if duration is not None else None,
            details=details,
        )

    async def transcribe(
        self,
        *,
        audio_ref: str | None = None,
        audio_b64: str | None = None,
        text_hint: str | None = None,
        language: str = "en",
        wake_mode: bool = False,
    ) -> Transcript:
        language = language or self.language or "en"
        path = await self._resolve_audio(audio_b64, audio_ref)
        try:
            return await asyncio.to_thread(
                self._transcribe_sync, path, language, wake_mode=wake_mode
            )
        except ModelUnavailableError:
            return Transcript(
                text="",
                confidence=0.0,
                language=language,
                provider=self.name,
                degraded=True,
                audio_ref=audio_ref,
                details={"reason": "weights-or-runtime-unavailable"},
            )
        finally:
            if path != audio_ref:
                os.unlink(path)

    def stream(
        self,
        *,
        audio_ref: str | None = None,
        audio_b64: str | None = None,
        text_hint: str | None = None,
        language: str = "en",
    ) -> AsyncIterator[Transcript | TranscriptPartial]:
        return _stream_final(
            self,
            audio_ref=audio_ref,
            audio_b64=audio_b64,
            text_hint=text_hint,
            language=language,
        )


# --------------------------------------------------------------------------- #
# Parakeet (ONNX, streaming, EOU) — default real ASR engine
# --------------------------------------------------------------------------- #


class ParakeetOnnxSession:
    """ONNX adapter for the NVIDIA NeMo Parakeet-EOU export.

    Expected graph contract (standard NeMo ONNX export):

    * inputs include ``audio_signal`` (float32 [1,T]) and
      ``audio_signal_length`` (int64 [1]); EOU exports also accept recurrent
      ``states`` tensors.
    * outputs include log-probabilities (name containing ``logprob``), final
      ``states``, and (for EOU exports) ``eou_probs``.

    Tokenization uses a sibling ``<model>.vocab.json`` (token -> id). Graphs or
    vocabularies that do not match raise ``ModelUnavailableError`` so the
    transcriber degrades instead of mislabeling output. Validate against the
    pulled weights on first install (see docs/VOICE.md).
    """

    def __init__(self, session, vocab_path: str | None = None) -> None:
        self._session = session
        self._input_names = [item.name for item in session.get_inputs()]
        self._output_names = [item.name for item in session.get_outputs()]
        self._vocab = self._load_vocab(vocab_path)
        self._state = None
        self._validate()

    def _validate(self) -> None:
        if not any("audio_signal" in name or name == "input" for name in self._input_names):
            raise ModelUnavailableError(
                "Parakeet ONNX graph has no audio_signal/input tensor"
            )
        if not self._output_names:
            raise ModelUnavailableError("Parakeet ONNX graph has no outputs")

    @staticmethod
    def _load_vocab(vocab_path: str | None) -> dict[int, str] | None:
        if not vocab_path or not os.path.isfile(vocab_path):
            return None
        import json

        with open(vocab_path, encoding="utf-8") as handle:
            raw = json.load(handle)
        if isinstance(raw, dict):
            # vocab.json maps token -> id.
            return {int(value): str(key) for key, value in raw.items()}
        return {index: str(token) for index, token in enumerate(raw)}

    def _greedy_text(self, logprobs) -> str:
        if self._vocab is None:
            raise ModelUnavailableError(
                "Parakeet vocab file (<model>.vocab.json) is missing; cannot decode tokens"
            )
        import numpy as np

        tokens = np.argmax(np.asarray(logprobs), axis=-1).reshape(-1)
        return "".join(self._vocab.get(int(token), "") for token in tokens).strip()

    @staticmethod
    def _confidence(logprobs) -> float:
        import numpy as np

        values = np.asarray(logprobs)
        if values.size == 0:
            return 0.0
        probs = np.exp(values - values.max(axis=-1, keepdims=True))
        probs = probs / probs.sum(axis=-1, keepdims=True)
        best = probs.max(axis=-1)
        return float(np.exp(np.mean(np.log(np.clip(best, 1e-12, 1.0)))))

    def _run(self, pcm: array.array, states=None):
        import numpy as np

        audio = np.asarray(pcm, dtype=np.float32).reshape(1, -1)
        feeds: dict = {}
        for name in self._input_names:
            if "audio_signal" in name or name == "input":
                feeds[name] = audio
            elif "length" in name:
                feeds[name] = np.asarray([audio.shape[1]], dtype=np.int64)
            elif "state" in name and states is not None:
                feeds[name] = states
        outputs = self._session.run(None, feeds)
        by_name = dict(zip(self._output_names, outputs, strict=False))
        logprobs = next(
            (by_name[name] for name in self._output_names if "logprob" in name.lower()),
            next(
                (
                    by_name[name]
                    for name in self._output_names
                    if "state" not in name.lower() and "eou" not in name.lower()
                ),
                outputs[0],
            ),
        )
        next_states = by_name.get("states", by_name.get("state"))
        eou = by_name.get("eou_probs", by_name.get("eou"))
        return logprobs, next_states, eou

    def transcribe(self, pcm: array.array, sample_rate: int) -> tuple[str, float]:
        logprobs, _states, _eou = self._run(pcm)
        return self._greedy_text(logprobs), self._confidence(logprobs)

    def reset(self) -> None:
        self._state = None

    def decode_chunk(self, pcm: array.array) -> tuple[str, bool]:
        """One streaming step: hypothesis + end-of-utterance flag."""

        logprobs, next_states, eou = self._run(pcm, self._state)
        self._state = next_states
        hypothesis = self._greedy_text(logprobs)
        triggered = False
        if eou is not None:
            import numpy as np

            triggered = bool(float(np.asarray(eou).reshape(-1)[-1]) > 0.5)
        return hypothesis, triggered

    def finalize(self) -> str:
        return ""


class ParakeetTranscriber:
    """Parakeet-EOU/TDT INT8 ONNX ASR with streaming partials.

    Loads through the ModelArbiter (on_demand slot, <=250 MB). When weights,
    onnxruntime, or the vocab are missing the provider returns a
    ``degraded=True`` transcript with confidence 0.0 — never a hint echo.
    """

    def __init__(
        self,
        *,
        name: str | None = None,
        model_name: str | None = None,
        model_path: str | None = None,
        vocab_path: str | None = None,
        vad_filter: bool | None = None,
        chunk_ms: int | None = None,
        session_factory=None,
    ) -> None:
        self.name = name or "parakeet-eou-120m"
        self.model_name = model_name or settings.voice_asr_engine
        self.model_path = model_path or _resolve_onnx_path(self.model_name)
        self.vocab_path = vocab_path or _resolve_vocab_path(self.model_name)
        self.vad_filter = (
            settings.voice_asr_vad_filter if vad_filter is None else vad_filter
        )
        self.chunk_ms = chunk_ms or settings.voice_asr_stream_chunk_ms
        self._session_factory = session_factory
        self._session = None

    @property
    def registry_name(self) -> str:
        return f"asr-{self.model_name}"

    def _load_session(self):
        if self._session is not None:
            return self._session
        if self._session_factory is not None:
            self._session = self._session_factory(self.model_path, self.vocab_path)
            return self._session
        if not self.model_path or not os.path.isfile(self.model_path):
            raise ModelUnavailableError(
                f"Parakeet ONNX weights not found at {self.model_path!r}; "
                "run `uv run python -m app.ml.cli pull asr-"
                f"{self.model_name}` (Agent 2 dependency)"
            )
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise ModelUnavailableError(
                "onnxruntime is not installed; install the ml extra (Agent 2 dependency)"
            ) from exc
        with acquire_model(self.registry_name):
            session = ort.InferenceSession(self.model_path, providers=["CPUExecutionProvider"])
        self._session = ParakeetOnnxSession(session, vocab_path=self.vocab_path)
        return self._session

    async def transcribe(
        self,
        *,
        audio_ref: str | None = None,
        audio_b64: str | None = None,
        text_hint: str | None = None,
        language: str = "en",
    ) -> Transcript:
        audio, _filename = await _read_audio(audio_b64, audio_ref)
        try:
            session = await asyncio.to_thread(self._load_session)
        except ModelUnavailableError:
            return Transcript(
                text="",
                confidence=0.0,
                language=language,
                provider=self.name,
                degraded=True,
                audio_ref=audio_ref,
                details={"reason": "weights-or-runtime-unavailable"},
            )
        pcm, rate = _wav_pcm(audio)
        if self.vad_filter:
            from app.audio.vad import default_vad_engine, segment_utterances

            segments = await segment_utterances(default_vad_engine(), pcm, sample_rate=rate)
            texts: list[str] = []
            confidences: list[float] = []
            for segment in segments:
                try:
                    text, confidence = await asyncio.to_thread(
                        session.transcribe, segment.samples, rate
                    )
                except Exception as exc:  # noqa: BLE001 - engine boundary -> typed error
                    raise VoiceError(
                        f"Parakeet ASR engine failed: {type(exc).__name__}: {exc}",
                        status=502,
                        code="asr_engine_error",
                    ) from exc
                if text:
                    texts.append(text)
                    confidences.append(confidence)
            if not texts:
                raise VoiceError(
                    "No speech detected in audio",
                    status=422,
                    code="asr_no_speech",
                )
            return Transcript(
                text=" ".join(texts),
                confidence=round(sum(confidences) / len(confidences), 4),
                language=language,
                provider=self.name,
                audio_ref=audio_ref,
                details={"engine": self.name, "vad": "on"},
            )
        try:
            text, confidence = await asyncio.to_thread(session.transcribe, pcm, rate)
        except VoiceError:
            raise
        except Exception as exc:  # noqa: BLE001 - engine boundary -> typed error
            raise VoiceError(
                f"Parakeet ASR engine failed: {type(exc).__name__}: {exc}",
                status=502,
                code="asr_engine_error",
            ) from exc
        if not text:
            raise VoiceError(
                "No speech detected in audio",
                status=422,
                code="asr_no_speech",
            )
        return Transcript(
            text=text,
            confidence=round(confidence, 4),
            language=language,
            provider=self.name,
            audio_ref=audio_ref,
            details={"engine": self.name, "vad": "off"},
        )

    async def _stream_partials(
        self,
        session,
        pcm: array.array,
    ) -> AsyncIterator[TranscriptPartial]:
        reset = getattr(session, "reset", None)
        if reset is not None:
            await asyncio.to_thread(reset)
        frame = max(1, int(16000 * self.chunk_ms / 1000))
        started = time.monotonic()
        sequence = 0
        hypothesis = ""
        for start in range(0, len(pcm), frame):
            chunk = pcm[start : start + frame]
            if not chunk:
                continue
            decode_chunk = getattr(session, "decode_chunk", None)
            if decode_chunk is None:
                break
            try:
                hypothesis, triggered = await asyncio.to_thread(decode_chunk, chunk)
            except Exception as exc:  # noqa: BLE001 - engine boundary -> typed error
                raise VoiceError(
                    f"Parakeet streaming ASR failed: {type(exc).__name__}: {exc}",
                    status=502,
                    code="asr_engine_error",
                ) from exc
            sequence += 1
            yield TranscriptPartial(
                text=hypothesis,
                provider=self.name,
                sequence=sequence,
                stable=triggered,
                confidence=0.0,
                timestamp_ms=int((time.monotonic() - started) * 1000),
            )
            if triggered:
                break
        finalize = getattr(session, "finalize", None)
        if finalize is not None:
            tail = await asyncio.to_thread(finalize)
            if tail:
                sequence += 1
                yield TranscriptPartial(
                    text=tail,
                    provider=self.name,
                    sequence=sequence,
                    stable=True,
                    confidence=0.0,
                    timestamp_ms=int((time.monotonic() - started) * 1000),
                )

    async def stream(
        self,
        *,
        audio_ref: str | None = None,
        audio_b64: str | None = None,
        text_hint: str | None = None,
        language: str = "en",
    ) -> AsyncIterator[Transcript | TranscriptPartial]:
        audio, _filename = await _read_audio(audio_b64, audio_ref)
        try:
            session = await asyncio.to_thread(self._load_session)
        except ModelUnavailableError:
            yield Transcript(
                text="",
                confidence=0.0,
                language=language,
                provider=self.name,
                degraded=True,
                audio_ref=audio_ref,
                details={"reason": "weights-or-runtime-unavailable"},
            )
            return
        pcm, rate = _wav_pcm(audio)
        if not hasattr(session, "decode_chunk"):
            yield await self.transcribe(
                audio_b64=audio_b64,
                audio_ref=audio_ref,
                language=language,
            )
            return
        last_hypothesis = ""
        async for partial in self._stream_partials(session, pcm):
            last_hypothesis = partial.text
            yield partial
        finalize = getattr(session, "finalize", None)
        final_text = (
            await asyncio.to_thread(finalize) if finalize is not None else last_hypothesis
        )
        text = final_text or last_hypothesis
        if not text:
            raise VoiceError(
                "No speech detected in audio",
                status=422,
                code="asr_no_speech",
            )
        yield Transcript(
            text=text,
            confidence=0.0,
            language=language,
            provider=self.name,
            audio_ref=audio_ref,
            details={"engine": self.name, "streaming": True},
        )


class ParakeetTdtTranscriber(ParakeetTranscriber):
    """Opt-in Parakeet-TDT-v3 accuracy tier (same streaming contract)."""

    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("name", "parakeet-tdt-v3")
        kwargs.setdefault("model_name", settings.voice_asr_alt_engine)
        super().__init__(**kwargs)


# --------------------------------------------------------------------------- #
# Qwen3-ASR-0.6B MLX: batch-only offline re-transcription (never resident)
# --------------------------------------------------------------------------- #


class QwenMlxRetranscriber:
    """Batch-only offline re-transcription via the mlx-lm CLI subprocess.

    The ~1 GB model is intentionally never loaded into the server process or
    the ModelArbiter; it is spawned as an external ``mlx_lm.generate`` process
    for an offline pass over stored audio. Missing binary/weights produce a
    degraded Transcript, never a fabricated one.
    """

    name = "qwen3-asr-0.6b-mlx"

    def __init__(self, model: str | None = None, binary: str = "mlx_lm.generate") -> None:
        self.model = model or "mlx-community/Qwen3-ASR-0.6B-4bit"
        self.binary = binary

    async def retranscribe(self, audio_ref: str, *, language: str = "en") -> Transcript:
        audio, filename = await _read_audio(None, audio_ref)
        with tempfile.NamedTemporaryFile(suffix=Path(filename).suffix or ".wav", delete=False) as handle:
            handle.write(audio)
            path = handle.name
        try:
            process = await asyncio.create_subprocess_exec(
                self.binary,
                "--model",
                self.model,
                "--prompt",
                "Transcribe the audio",
                "--audio",
                path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _stderr = await process.communicate()
            text = stdout.decode("utf-8", errors="replace").strip()
        except (FileNotFoundError, OSError):
            return Transcript(
                text="",
                confidence=0.0,
                language=language,
                provider=self.name,
                degraded=True,
                audio_ref=audio_ref,
                details={"reason": "mlx-binary-or-weights-unavailable"},
            )
        finally:
            os.unlink(path)
        if not text:
            return Transcript(
                text="",
                confidence=0.0,
                language=language,
                provider=self.name,
                degraded=True,
                audio_ref=audio_ref,
                details={"reason": "empty-subprocess-output"},
            )
        return Transcript(
            text=text,
            confidence=0.0,
            language=language,
            provider=self.name,
            audio_ref=audio_ref,
            details={"engine": self.name, "offline": True},
        )


# --------------------------------------------------------------------------- #
# Arbiter + factory
# --------------------------------------------------------------------------- #


def _resolve_onnx_path(model_name: str) -> str:
    explicit = getattr(settings, "voice_asr_onnx_path", None)
    if explicit:
        return explicit
    candidates = []
    if settings.voice_asr_model_dir:
        candidates.append(Path(settings.voice_asr_model_dir) / f"{model_name}.onnx")
    candidates.append(Path.home() / ".ev" / "models" / f"asr-{model_name}.onnx")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return str(candidates[-1])


def _resolve_vocab_path(model_name: str) -> str | None:
    base = Path(_resolve_onnx_path(model_name))
    vocab = base.with_suffix(".vocab.json")
    return str(vocab) if vocab.is_file() else None


def get_transcriber() -> Transcriber:
    provider = settings.voice_asr_provider
    if provider == "openai_compat":
        if not settings.voice_asr_base_url:
            raise RuntimeError("EV_VOICE_ASR_BASE_URL is required for openai_compat ASR")
        if not remote_processing_allowed("voice_asr"):
            raise RuntimeError(
                "Remote ASR is denied by regional policy; set EV_ALLOW_REMOTE_ASR=true"
            )
        return OpenAICompatTranscriber(
            base_url=settings.voice_asr_base_url,
            api_key=settings.voice_asr_api_key,
            model=settings.voice_asr_model,
        )
    if provider == "faster_whisper":
        return FasterWhisperTranscriber(vad_filter=settings.voice_asr_vad_filter)
    if provider == "parakeet":
        return ParakeetTranscriber()
    if provider in ("parakeet_tdt", "parakeet-tdt"):
        return ParakeetTdtTranscriber()
    if provider == "echo":
        return EchoTranscriber()
    raise RuntimeError(f"unknown voice_asr_provider {provider!r}")
