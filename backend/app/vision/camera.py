"""Single-frame camera capture through the ``evvision`` Swift helper.

Every capture is an explicit, logged, consented action. There is no
background stream: ``capture_once`` grabs exactly one frame and discards it
unless the caller opts in to persistence for that capture.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.vision.providers import (
    VisionBinaryError,
    VisionEngineError,
    VisionProviderError,
    find_evvision_binary,
)
from app.vision.settings import get_vision_settings

logger = logging.getLogger(__name__)


class CameraPermissionDeniedError(VisionProviderError):
    """macOS denied camera permission for this single-frame capture."""


@dataclass
class CameraCaptureResult:
    provider: str = "camera_capture"
    device: str | None = None
    captured: bool = False
    persisted: bool = False
    persist_path: str | None = None
    pixel_count: int = 0


async def capture_once(
    *,
    persist: bool = False,
    output_path: str | Path | None = None,
    binary: str | None = None,
    consent_reason: str = "explicit user request",
    timeout: float | None = None,
) -> CameraCaptureResult:
    """Grab one camera frame on explicit request only."""

    if persist and output_path is None:
        raise ValueError("persist=True requires output_path for a single-frame capture")
    settings = get_vision_settings()
    binary = find_evvision_binary(binary)
    timeout = timeout or settings.vision_capture_timeout
    logger.info("camera capture once: %s", consent_reason)
    command = [binary, "camera", "--once"]
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
        raise VisionEngineError(f"camera capture timed out after {timeout:.0f}s") from exc

    if proc.returncode == 3:
        try:
            payload = json.loads(proc.stdout)
            message = (payload.get("error") or {}).get("message") or "permission denied"
        except json.JSONDecodeError:
            message = proc.stdout.strip() or "permission denied"
        raise CameraPermissionDeniedError(message)
    if proc.returncode != 0:
        try:
            payload = json.loads(proc.stdout)
            message = (payload.get("error") or {}).get("message") or "capture failed"
        except json.JSONDecodeError:
            message = proc.stdout.strip() or proc.stderr.strip() or "capture failed"
        raise VisionEngineError(f"{binary} camera capture failed: {message}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise VisionEngineError(f"{binary} returned unparseable camera output") from exc
    return CameraCaptureResult(
        device=payload.get("device"),
        captured=bool(payload.get("captured")),
        persisted=bool(payload.get("persisted")),
        persist_path=payload.get("persist_path"),
        pixel_count=int(payload.get("pixel_count") or 0),
    )
