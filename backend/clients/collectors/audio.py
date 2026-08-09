"""Derived audio-scene hints (no raw audio ever leaves the device).

Real scene classification belongs to on-device ML; this collector only relays
derived hints that the OS or a local classifier has already produced (via
``EV_AUDIO_SCENE`` / ``EV_IN_CALL`` or a user-managed ``~/.ev/audio-scene.json``).
"""

from __future__ import annotations

import json
import os
from contextlib import suppress
from pathlib import Path


def _local_hint_file() -> Path:
    return Path(os.environ.get("EV_AUDIO_SCENE_FILE", str(Path.home() / ".ev" / "audio-scene.json")))


def audio_scene() -> dict | None:
    scene = os.environ.get("EV_AUDIO_SCENE")
    in_call = os.environ.get("EV_IN_CALL")
    confidence = os.environ.get("EV_AUDIO_CONFIDENCE")
    if scene is None and in_call is None:
        try:
            data = json.loads(_local_hint_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        scene = data.get("scene")
        in_call = data.get("in_call")
        confidence = data.get("confidence")
    if scene is None and in_call is None:
        return None
    payload: dict = {}
    if scene:
        payload["scene"] = str(scene)[:64]
    if in_call is not None:
        payload["in_call"] = str(in_call).lower() in {"1", "true", "yes"}
    if confidence is not None:
        with suppress(TypeError, ValueError):
            payload["confidence"] = max(0.0, min(1.0, float(confidence)))
    return payload or None
