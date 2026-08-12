"""Env-backed policy for the ML runtime.

Kept separate from ``app.config`` so the ML foundation can be imported and
tested without loading the API/database configuration.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MLSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EV_",
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )

    ml_model_dir: Path = Field(
        default=Path.home() / ".ev" / "models",
        validation_alias=AliasChoices("EV_MODEL_DIR", "EV_ML_MODEL_DIR"),
    )
    ml_dataset_dir: Path = Path.home() / ".ev" / "datasets"
    # Hard ceiling on resident model memory (MB). A load that would exceed this
    # is refused, never silently swapped.
    ml_resident_ceiling_mb: int = Field(default=2400, ge=1)
    # Refuse downloads when free disk drops below this threshold (GB).
    ml_min_free_gb: float = Field(default=5.0, ge=0.0)
    # Shared budget for all on-demand models (MB), evicted LRU.
    ml_on_demand_slot_mb: int = Field(default=600, ge=1)
    # Registry-level upper bound for exclusive-tier models (MB).
    ml_exclusive_limit_mb: int = Field(default=3500, ge=1)
    # Deployment posture: api-first (recommended, only the four justified local
    # models) or local (adds the optional MLX trainer). auto derives from the
    # installed extras.
    ml_posture: Literal["auto", "api-first", "local"] = "auto"


@lru_cache
def get_ml_settings() -> MLSettings:
    return MLSettings()


settings = get_ml_settings()
