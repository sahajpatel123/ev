"""Dataset registry: license-first manifests, eval-only enforcement."""

from app.datasets.registry import (
    DatasetEvalOnlyError,
    DatasetRegistry,
    DatasetSpec,
    builtin_datasets,
)

__all__ = [
    "DatasetEvalOnlyError",
    "DatasetRegistry",
    "DatasetSpec",
    "builtin_datasets",
]
