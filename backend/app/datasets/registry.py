"""Declarative dataset registry with license and eval-only discipline."""

from __future__ import annotations

import threading

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DatasetRegistryError(ValueError):
    """Invalid dataset registry state or entry."""


class DatasetLicenseError(DatasetRegistryError):
    """A dataset without a license was refused registration."""


class DatasetEvalOnlyError(RuntimeError):
    """An eval-only dataset was used outside an explicit evaluation context."""


class DatasetNotFoundError(DatasetRegistryError, KeyError):
    """Requested dataset is not registered."""


class DatasetSpec(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    name: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    description: str = ""
    source_url: str | None = None
    sha256: str | None = None
    bytes: int = Field(default=0, ge=0)
    eval_only: bool = False
    streaming: bool = False
    license: str = Field(min_length=1)
    license_url: str | None = None
    verified: bool = False

    @field_validator("sha256")
    @classmethod
    def _normalize_sha256(cls, value: str | None) -> str | None:
        if value is None:
            return None
        digest = value.lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("sha256 must be 64 lowercase hex characters")
        return digest

    @model_validator(mode="after")
    def _validate_entry(self) -> DatasetSpec:
        if not self.license.strip():
            raise ValueError("license is required")
        return self


class DatasetRegistry:
    """Process-local dataset registry."""

    def __init__(self) -> None:
        self._specs: dict[str, DatasetSpec] = {}
        self._lock = threading.RLock()

    def register(self, spec: DatasetSpec) -> DatasetSpec:
        with self._lock:
            if not spec.license.strip():
                raise DatasetLicenseError(
                    f"dataset {spec.name!r} has no license and cannot be registered"
                )
            if spec.name in self._specs:
                raise DatasetRegistryError(f"dataset {spec.name!r} is already registered")
            self._specs[spec.name] = spec
            return spec

    def get(self, name: str) -> DatasetSpec:
        with self._lock:
            try:
                return self._specs[name]
            except KeyError as exc:
                raise DatasetNotFoundError(f"dataset {name!r} is not registered") from exc

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._specs)

    def all(self) -> list[DatasetSpec]:
        with self._lock:
            return sorted(self._specs.values(), key=lambda spec: spec.name)


def guard_eval(spec: DatasetSpec, *, eval_context: bool) -> None:
    """Enforce the eval_only flag: non-eval consumers are refused."""

    if spec.eval_only and not eval_context:
        raise DatasetEvalOnlyError(
            f"dataset {spec.name!r} is eval_only; it may only be used with "
            "eval_context=True"
        )


def builtin_datasets() -> list[DatasetSpec]:
    """Seed manifests (metadata only; nothing is downloaded at registration).

    Checksums are intentionally unpinned: a maintainer must pin and verify each
    sha256 before ``pull`` will download.
    """

    return [
        DatasetSpec(
            name="librispeech_test_clean",
            description="LibriSpeech test-clean subset: ~5.4h of read English speech",
            source_url="https://www.openslr.org/resources/12/test-clean.tar.gz",
            bytes=346_188_340,
            eval_only=True,
            streaming=False,
            license="CC BY 4.0",
            license_url="https://www.openslr.org/12/",
        ),
        DatasetSpec(
            name="voxceleb1_o_cleaned_trials",
            description="VoxCeleb1-O cleaned trial-pair subset for speaker verification EER",
            source_url="https://www.robots.ox.ac.uk/~vgg/data/voxceleb/vox1a/vox1_test_wav.zip",
            bytes=1_148_000_000,
            eval_only=True,
            streaming=False,
            license="VoxCeleb research license (non-commercial)",
            license_url="https://www.robots.ox.ac.uk/~vgg/data/voxceleb/",
        ),
        DatasetSpec(
            name="esc50",
            description="ESC-50: 2,000 labeled environmental audio clips (50 classes)",
            source_url="https://github.com/karolpiczak/ESC-50/archive/refs/heads/master.zip",
            bytes=662_000_000,
            eval_only=True,
            streaming=False,
            license="CC BY-NC 4.0",
            license_url="https://github.com/karolpiczak/ESC-50",
        ),
        DatasetSpec(
            name="lfw",
            description="Labeled Faces in the Wild: 13,233 face images",
            source_url="http://vis-www.cs.umass.edu/lfw/lfw.tgz",
            bytes=233_000_000,
            eval_only=True,
            streaming=False,
            license="LFW non-commercial research license",
            license_url="https://vis-www.cs.umass.edu/lfw/",
        ),
        DatasetSpec(
            name="audioset_balanced_eval",
            description="AudioSet balanced evaluation segment manifest (CSV)",
            source_url="https://storage.googleapis.com/us_audioset/youtube_corpus/v1/csv/eval_segments.csv",
            bytes=26_000_000,
            eval_only=True,
            streaming=True,
            license="CC BY 4.0 (annotations)",
            license_url="https://research.google.com/audioset/dataset/index.html",
        ),
    ]
