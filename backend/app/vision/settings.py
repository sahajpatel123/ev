"""Env-backed settings for the Agent 6 vision stack.

Kept separate from ``app.config`` so vision code can be imported without
loading the API/database configuration, and so this agent never has to modify
the shared Settings class.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class VisionSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EV_", env_file=".env", extra="ignore")

    # Apple Vision helper (helpers/evvision): binary name or absolute path.
    vision_evvision_binary: str = "evvision"
    # Auto-select Apple Vision as the Darwin default when the binary exists.
    vision_evvision_auto: bool = True

    # Local on-device perception engines. "auto" uses ONNX when the model and
    # runtime are present, otherwise the honest deterministic double.
    vision_detect_engine: str = "auto"
    vision_detect_model: str = "detect-rtdetr-nano"
    vision_scene_engine: str = "auto"
    vision_scene_model: str = "scene-mobileclip-s0"
    vision_face_engine: str = "auto"
    vision_face_model: str = "face-yunet"

    # Screen capture privacy: default privacy level and downscale cap.
    vision_screen_privacy_level: str = "sensitive"
    vision_screen_max_dimension: int = 1280
    vision_capture_timeout: float = 30.0


@lru_cache
def get_vision_settings() -> VisionSettings:
    return VisionSettings()
