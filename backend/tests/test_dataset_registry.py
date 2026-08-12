"""Dataset registry: license discipline, eval-only guard, lifecycle."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.datasets import registry, store
from app.datasets.registry import (
    DatasetEvalOnlyError,
    DatasetRegistry,
    DatasetSpec,
    builtin_datasets,
    guard_eval,
)
from app.ml.registry import ChecksumError
from app.ml.settings import MLSettings


def make_settings(tmp_path: Path) -> MLSettings:
    return MLSettings(
        _env_file=None,
        ml_model_dir=tmp_path / "models",
        ml_dataset_dir=tmp_path / "datasets",
        ml_min_free_gb=0.0,
    )


def make_spec(name: str, *, eval_only: bool = False, source_url: str | None = None, sha256: str | None = None) -> DatasetSpec:
    return DatasetSpec(
        name=name,
        source_url=source_url,
        sha256=sha256,
        bytes=len("data"),
        eval_only=eval_only,
        license="CC BY 4.0",
    )


def test_dataset_without_license_cannot_be_registered() -> None:
    with pytest.raises(ValidationError):
        DatasetSpec(name="x", license="")
    reg = DatasetRegistry()
    spec = DatasetSpec.model_construct(name="x", license="")
    with pytest.raises(registry.DatasetLicenseError, match="no license"):
        reg.register(spec)


def test_eval_only_flag_enforced_in_code() -> None:
    spec = make_spec("eval-set", eval_only=True)
    with pytest.raises(DatasetEvalOnlyError, match="eval_only"):
        guard_eval(spec, eval_context=False)
    guard_eval(spec, eval_context=True)


def test_seed_manifests_are_eval_only_and_esc50_is_cc_by_nc() -> None:
    specs = {item.name: item for item in builtin_datasets()}
    assert {
        "librispeech_test_clean",
        "voxceleb1_o_cleaned_trials",
        "esc50",
        "lfw",
        "audioset_balanced_eval",
    } <= set(specs)
    assert specs["esc50"].eval_only
    assert "CC BY-NC" in specs["esc50"].license
    assert all(item.eval_only for item in specs.values())
    assert all(item.license for item in specs.values())


def test_pull_verified_local_dataset(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    source = tmp_path / "data.csv"
    source.write_bytes(b"id,label\n1,cat\n")
    spec = make_spec(
        "mini",
        source_url=source.as_uri(),
        sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
    )
    path = store.download_dataset(spec, settings)
    assert path.exists()
    assert path.read_bytes() == source.read_bytes()


def test_corrupt_dataset_checksum_rejected_and_removed(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    source = tmp_path / "data.csv"
    source.write_bytes(b"bad")
    spec = make_spec("mini", source_url=source.as_uri(), sha256="0" * 64)
    with pytest.raises(ChecksumError, match="sha256 mismatch"):
        store.download_dataset(spec, settings)
    assert not list(settings.ml_dataset_dir.glob("*"))


def test_use_dataset_enforces_eval_only_before_download(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    reg = DatasetRegistry()
    source = tmp_path / "data.csv"
    source.write_bytes(b"x")
    reg.register(
        make_spec(
            "eval",
            eval_only=True,
            source_url=source.as_uri(),
            sha256=hashlib.sha256(b"x").hexdigest(),
        )
    )
    with pytest.raises(DatasetEvalOnlyError), store.use_dataset("eval", reg, settings):
        pass
    assert not list(settings.ml_dataset_dir.glob("*"))


def test_use_dataset_deletes_after_use(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    reg = DatasetRegistry()
    source = tmp_path / "data.csv"
    source.write_bytes(b"payload")
    reg.register(
        make_spec(
            "temp",
            source_url=source.as_uri(),
            sha256=hashlib.sha256(b"payload").hexdigest(),
        )
    )
    with store.use_dataset("temp", reg, settings, delete_after_use=True) as path:
        assert path.exists()
    assert not path.exists()


def test_dataset_prune_removes_artifacts(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    cache = settings.ml_dataset_dir
    cache.mkdir(parents=True)
    (cache / "a.csv").write_bytes(b"a")
    (cache / "b.csv").write_bytes(b"b")
    removed = store.prune_datasets(settings, all_files=True)
    assert len(removed) == 2
    assert not list(cache.iterdir())
