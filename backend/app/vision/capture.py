"""macOS screen pixel capture through the ``evvision`` Swift helper.

Privacy contract:
- Default ``privacy_level`` is ``sensitive``.
- The helper captures only the frontmost window, downscales before OCR, and
  discards pixels immediately.
- Raw frames are never persisted unless the caller explicitly opts in per
  capture (``persist=True`` plus an output path).
- Denied Screen Recording permission surfaces as
  :class:`ScreenRecordingDeniedError`, never as an empty success.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from app.vision.providers import (
    VisionBinaryError,
    VisionEngineError,
    VisionProviderError,
    find_evvision_binary,
)
from app.vision.settings import get_vision_settings


class ScreenRecordingDeniedError(VisionProviderError):
    """macOS denied Screen Recording permission for this capture."""


@dataclass
class ScreenCaptureResult:
    provider: str = "screen_capture"
    app: str | None = None
    window: str | None = None
    captured: bool = False
    persisted: bool = False
    persist_path: str | None = None
    pixel_count: int = 0
    ocr_text: str | None = None
    ocr_lines: list[dict] = field(default_factory=list)
    privacy_level: str = "sensitive"


def _raise_for_status(proc: subprocess.CompletedProcess, binary: str) -> dict:
    if proc.returncode == 3:
        try:
            payload = json.loads(proc.stdout)
            message = (payload.get("error") or {}).get("message") or "permission denied"
        except json.JSONDecodeError:
            message = proc.stdout.strip() or "permission denied"
        raise ScreenRecordingDeniedError(message)
    if proc.returncode != 0:
        try:
            payload = json.loads(proc.stdout)
            message = (payload.get("error") or {}).get("message") or "capture failed"
        except json.JSONDecodeError:
            message = proc.stdout.strip() or proc.stderr.strip() or "capture failed"
        raise VisionEngineError(f"{binary} screen capture failed: {message}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise VisionEngineError(f"{binary} returned unparseable screen capture output") from exc


async def capture_frontmost_window(
    *,
    persist: bool = False,
    output_path: str | Path | None = None,
    binary: str | None = None,
    privacy_level: str | None = None,
    timeout: float | None = None,
) -> ScreenCaptureResult:
    """Capture and OCR the frontmost window; never persist without opt-in."""

    settings = get_vision_settings()
    binary = find_evvision_binary(binary)
    timeout = timeout or settings.vision_capture_timeout
    privacy_level = privacy_level or settings.vision_screen_privacy_level
    command = [binary, "screen"]
    if persist and output_path is not None:
        command += ["--persist", str(output_path)]
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise VisionBinaryError(
            f"Apple Vision helper binary {binary!r} not found; "
            "build it with `swift build -c release` in helpers/evvision"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise VisionEngineError(f"screen capture timed out after {timeout:.0f}s") from exc

    payload = _raise_for_status(proc, binary)
    ocr = payload.get("ocr") or {}
    return ScreenCaptureResult(
        app=payload.get("app"),
        window=payload.get("window"),
        captured=bool(payload.get("captured")),
        persisted=bool(payload.get("persisted")),
        persist_path=payload.get("persist_path"),
        pixel_count=int(payload.get("pixel_count") or 0),
        ocr_text=ocr.get("text"),
        ocr_lines=ocr.get("lines") or [],
        privacy_level=privacy_level,
    )
