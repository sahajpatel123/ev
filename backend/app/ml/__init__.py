"""EV ML runtime foundation: registry, budget arbiter, device selection.

This package owns the *policy* for models: which models exist, how much
resident memory they may claim, and which backend should run them. It does not
implement perception or voice engines.
"""

from app.ml.arbiter import ModelArbiter, ModelLoadRefused
from app.ml.device import select_backend
from app.ml.registry import ModelRegistry, ModelSpec, ModelTier, builtin_models

__all__ = [
    "ModelArbiter",
    "ModelLoadRefused",
    "ModelRegistry",
    "ModelSpec",
    "ModelTier",
    "builtin_models",
    "select_backend",
]
