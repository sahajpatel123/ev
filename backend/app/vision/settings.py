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

    # Clip ingest: recorded video upload cap and sampled keyframes per clip.
    # Extraction shells out to ffmpeg; without it every clip degrades honestly.
    vision_clip_max_mb: int = 64
    vision_clip_max_frames: int = 6

    # Local on-device perception engines. "auto" uses ONNX when the model and
    # runtime are present, otherwise the honest deterministic double.
    vision_detect_engine: str = "auto"
    vision_detect_model: str = "detect-rtdetr-v2-r18vd"
    vision_scene_engine: str = "auto"
    vision_scene_model: str = "scene-mobileclip-s0"
    vision_face_engine: str = "auto"
    vision_face_model: str = "face-yunet"

    # Phone look frames: photo captures always store their pixels; ordinary
    # looks and bursts store pixels only when the owner opts in here. Kept
    # bounded by the media retention sweep either way.
    vision_store_look_pixels: bool = False

    # Screen capture privacy: default privacy level and downscale cap.
    vision_screen_privacy_level: str = "sensitive"
    vision_screen_max_dimension: int = 1280
    vision_capture_timeout: float = 30.0

    # Self-hosted DeepSeek-OCR HTTP endpoint. Official api.deepseek.com is
    # text-only and is refused by DeepSeekOCRProvider.
    vision_deepseek_ocr_url: str | None = None
    vision_deepseek_ocr_timeout: float = 20.0


@lru_cache
def get_vision_settings() -> VisionSettings:
    return VisionSettings()
