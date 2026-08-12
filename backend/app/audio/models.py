"""Shared ModelArbiter access for Agent 3 engines.

Every real model loaded by the ears stack (Silero VAD, YAMNet, the custom
openWakeWord head) must go through the arbiter so the memory budget in
docs/MODEL_BUDGET.md is enforced process-wide. One arbiter instance is shared
across all ears modules.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from app.ml.arbiter import ModelArbiter, ModelLoadRefused, create_default_arbiter

__all__ = ["ModelArbiter", "ModelLoadRefused", "acquire_model", "model_arbiter"]


@lru_cache(maxsize=1)
def model_arbiter() -> ModelArbiter:
    return create_default_arbiter()


@contextmanager
def acquire_model(name: str) -> Iterator[None]:
    """Reserve ``name`` in the arbiter for the duration of a model load."""

    with model_arbiter().acquire(name):
        yield
