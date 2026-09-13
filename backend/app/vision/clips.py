"""Clip ingest: keyframes and audio from a recorded video, honestly degraded.

A clip is only as useful as the moments we can look at and the speech we can
read. This module owns that extraction and nothing else: no memory writes, no
model calls, no policy. It shells out to ``ffmpeg``/``ffprobe`` (present on the
owner's Mac, used already for audio) and returns plain dataclasses.

When ``ffmpeg`` is absent, or a clip cannot be decoded, the result is
``degraded=True`` with **no frames** — never a fabricated frame, never a guessed
duration. Callers must treat an empty frame list as "nothing was seen".

Determinism matters: frame timestamps come from an even grid over the probed
duration, so the same clip always yields the same moments.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("ev.vision.clips")

MAX_FRAMES = 6
MAX_FRAME_DIMENSION = 1280
MAX_CLIP_SECONDS = 120.0
FRAME_TIMEOUT_SECONDS = 12.0
PROBE_TIMEOUT_SECONDS = 8.0
AUDIO_TIMEOUT_SECONDS = 30.0
AUDIO_SAMPLE_RATE = 16_000
SUPPORTED_SUFFIXES = {".mov", ".mp4", ".m4v", ".qt", ".webm", ".avi", ".mkv"}


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def ffprobe_path() -> str | None:
    return shutil.which("ffprobe")


def extraction_available() -> bool:
    return bool(ffmpeg_path() and ffprobe_path())


def clip_suffix(filename: str | None, content_type: str | None = None) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix in SUPPORTED_SUFFIXES:
        return suffix
    mime = (content_type or "").lower()
    if "quicktime" in mime:
        return ".mov"
    if "webm" in mime:
        return ".webm"
    if "avi" in mime:
        return ".avi"
    return ".mp4"


@dataclass
class ClipProbe:
    duration_s: float | None = None
    has_audio: bool = False
    width: int | None = None
    height: int | None = None
    engine: str = "none"
    degraded: bool = True
    error: str | None = None


@dataclass
class ClipFrame:
    t_ms: int
    jpeg: bytes
    width: int | None = None
    height: int | None = None


@dataclass
class ClipFrames:
    frames: list[ClipFrame] = field(default_factory=list)
    probe: ClipProbe = field(default_factory=ClipProbe)
    engine: str = "none"
    degraded: bool = True
    error: str | None = None

    @property
    def duration_s(self) -> float | None:
        return self.probe.duration_s


def _write_temp(data: bytes, suffix: str) -> str:
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        return tmp.name


def _run(args: list[str], *, timeout: float, binary_output: bool = True):
    return subprocess.run(
        args,
        capture_output=True,
        timeout=timeout,
        check=False,
        stdin=subprocess.DEVNULL,
        text=not binary_output,
    )


def _probe_sync(data: bytes, suffix: str) -> ClipProbe:
    ffprobe = ffprobe_path()
    if not ffprobe:
        return ClipProbe(error="ffprobe_unavailable")
    path = _write_temp(data, suffix)
    try:
        proc = _run(
            [
                ffprobe,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                path,
            ],
            timeout=PROBE_TIMEOUT_SECONDS,
            binary_output=False,
        )
        if proc.returncode != 0:
            return ClipProbe(error="probe_failed")
        payload = json.loads(proc.stdout or "{}")
        streams = payload.get("streams") or []
        duration = None
        raw_duration = (payload.get("format") or {}).get("duration")
        try:
            if raw_duration is not None:
                duration = float(raw_duration)
        except (TypeError, ValueError):
            duration = None
        width = height = None
        for stream in streams:
            if str(stream.get("codec_type")) != "video":
                continue
            try:
                width = int(stream.get("width")) if stream.get("width") else None
                height = int(stream.get("height")) if stream.get("height") else None
            except (TypeError, ValueError):
                width = height = None
            break
        if duration is None:
            for stream in streams:
                try:
                    if stream.get("duration") is not None:
                        duration = float(stream["duration"])
                        break
                except (TypeError, ValueError):
                    continue
        if duration is not None:
            duration = max(0.0, min(float(duration), MAX_CLIP_SECONDS))
        has_audio = any(str(s.get("codec_type")) == "audio" for s in streams)
        return ClipProbe(
            duration_s=duration,
            has_audio=has_audio,
            width=width,
            height=height,
            engine="ffprobe",
            degraded=False,
        )
    except Exception as exc:  # noqa: BLE001 - probe failure must degrade, not raise
        logger.info("clip probe failed: %s", type(exc).__name__)
        return ClipProbe(error="probe_failed")
    finally:
        Path(path).unlink(missing_ok=True)


def frame_times(duration_s: float | None, count: int) -> list[float]:
    """Even grid over the clip, at most ``count`` samples, never past the end."""

    total = max(1, min(int(count), MAX_FRAMES))
    if duration_s is None or duration_s <= 0.05:
        return [0.0]
    if duration_s < 1.5:
        return [round(duration_s * 0.45, 3)]
    span = max(duration_s - 0.05, 0.0)
    if total == 1:
        return [round(span * 0.5, 3)]
    return [round(span * (index + 0.5) / total, 3) for index in range(total)]


def _frame_args(ffmpeg: str, path: str, seconds: float) -> list[str]:
    return [
        ffmpeg,
        "-nostdin",
        "-v",
        "error",
        "-ss",
        f"{seconds:.3f}",
        "-i",
        path,
        "-frames:v",
        "1",
        "-vf",
        f"scale='min({MAX_FRAME_DIMENSION},iw)':-2",
        "-q:v",
        "4",
        "-f",
        "image2",
        "-c:v",
        "mjpeg",
        "pipe:1",
    ]


def _extract_sync(data: bytes, suffix: str, count: int) -> ClipFrames:
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        return ClipFrames(error="ffmpeg_unavailable", engine="none")
    probe = _probe_sync(data, suffix)
    if probe.error:
        return ClipFrames(probe=probe, error=probe.error, engine="ffmpeg")
    path = _write_temp(data, suffix)
    frames: list[ClipFrame] = []
    try:
        for seconds in frame_times(probe.duration_s, count):
            try:
                proc = _run(_frame_args(ffmpeg, path, seconds), timeout=FRAME_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                logger.info("clip frame extraction timed out at %.2fs", seconds)
                continue
            if proc.returncode != 0 or not proc.stdout:
                continue
            raw = proc.stdout
            if not raw.startswith(b"\xff\xd8"):
                continue
            frames.append(ClipFrame(t_ms=int(round(seconds * 1000)), jpeg=raw))
    except Exception as exc:  # noqa: BLE001 - extraction must degrade, not raise
        logger.info("clip frame extraction failed: %s", type(exc).__name__)
        return ClipFrames(frames=frames, probe=probe, engine="ffmpeg", error="extract_failed")
    finally:
        Path(path).unlink(missing_ok=True)
    return ClipFrames(
        frames=frames,
        probe=probe,
        engine="ffmpeg",
        degraded=not frames,
        error=None if frames else "no_frames",
    )


async def probe_clip(data: bytes, *, filename: str | None = None, content_type: str | None = None) -> ClipProbe:
    return await asyncio.to_thread(_probe_sync, data, clip_suffix(filename, content_type))


async def extract_frames(
    data: bytes,
    *,
    count: int = MAX_FRAMES,
    filename: str | None = None,
    content_type: str | None = None,
) -> ClipFrames:
    """Keyframes on an even time grid. Empty + degraded when undecodable."""

    return await asyncio.to_thread(
        _extract_sync, data, clip_suffix(filename, content_type), count
    )


def _audio_args(ffmpeg: str, path: str, max_seconds: float) -> list[str]:
    return [
        ffmpeg,
        "-nostdin",
        "-v",
        "error",
        "-i",
        path,
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(AUDIO_SAMPLE_RATE),
        "-t",
        f"{max_seconds:.2f}",
        "-f",
        "wav",
        "pipe:1",
    ]


def _extract_audio_sync(data: bytes, suffix: str, max_seconds: float) -> bytes | None:
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        return None
    path = _write_temp(data, suffix)
    try:
        proc = _run(_audio_args(ffmpeg, path, max_seconds), timeout=AUDIO_TIMEOUT_SECONDS)
        if proc.returncode != 0 or not proc.stdout:
            return None
        return proc.stdout
    except Exception:  # noqa: BLE001 - silent clips are normal
        return None
    finally:
        Path(path).unlink(missing_ok=True)


async def extract_audio_wav(
    data: bytes,
    *,
    filename: str | None = None,
    content_type: str | None = None,
    max_seconds: float = MAX_CLIP_SECONDS,
) -> bytes | None:
    """16 kHz mono PCM WAV for ASR, or None when the clip has no audio."""

    return await asyncio.to_thread(
        _extract_audio_sync,
        data,
        clip_suffix(filename, content_type),
        max(1.0, min(float(max_seconds), MAX_CLIP_SECONDS)),
    )
