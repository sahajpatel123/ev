"""Model registry + download store acceptance tests."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.ml import cli, registry, store
from app.ml.registry import (
    ChecksumError,
    DiskGuardError,
    ModelRegistry,
    ModelSpec,
    ModelTier,
    RegistryError,
    builtin_models,
)
from app.ml.settings import MLSettings, get_ml_settings


def make_settings(tmp_path: Path, *, min_free_gb: float = 0.0) -> MLSettings:
    return MLSettings(
        _env_file=None,
        ml_model_dir=tmp_path / "models",
        ml_dataset_dir=tmp_path / "datasets",
        ml_min_free_gb=min_free_gb,
    )


def test_model_dir_accepts_ev_model_dir_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EV_MODEL_DIR", str(tmp_path / "models"))
    settings = MLSettings(_env_file=None)
    assert settings.ml_model_dir == tmp_path / "models"


def make_spec(
    name: str,
    *,
    tier: ModelTier = ModelTier.ON_DEMAND,
    resident_mb: int = 10,
    source_url: str | None = None,
    sha256: str | None = None,
    license: str = "MIT",
) -> ModelSpec:
    return ModelSpec(
        name=name,
        task="test",
        source_url=source_url,
        sha256=sha256,
        disk_mb=resident_mb,
        resident_mb=resident_mb,
        peak_mb=resident_mb,
        tier=tier,
        license=license,
    )


def test_model_without_license_cannot_be_registered() -> None:
    with pytest.raises(ValidationError):
        ModelSpec(
            name="x",
            task="t",
            disk_mb=1,
            resident_mb=1,
            peak_mb=1,
            tier=ModelTier.ON_DEMAND,
            license="",
        )
    reg = ModelRegistry()
    spec = ModelSpec.model_construct(
        name="x",
        task="t",
        disk_mb=1,
        resident_mb=1,
        peak_mb=1,
        tier=ModelTier.ON_DEMAND,
        license="",
    )
    with pytest.raises(registry.LicenseError, match="no license"):
        reg.register(spec)


def test_sha256_must_be_hex_when_present() -> None:
    with pytest.raises(ValidationError):
        make_spec("bad-hash", sha256="not-a-hash")


def test_duplicate_registration_refused() -> None:
    reg = ModelRegistry()
    reg.register(make_spec("dup"))
    with pytest.raises(RegistryError, match="already registered"):
        reg.register(make_spec("dup"))


def test_builtin_roster_matches_budget_law() -> None:
    specs = builtin_models()
    expected_always = [
        spec for spec in specs if spec.tier is ModelTier.ALWAYS and not spec.optional
    ]
    assert sum(spec.resident_mb for spec in expected_always) == 64
    for spec in specs:
        assert spec.license
        if spec.tier is ModelTier.EXCLUSIVE:
            assert spec.resident_mb <= 3500
        if spec.tier is ModelTier.ON_DEMAND:
            assert spec.resident_mb <= 600
    by_name = {spec.name: spec for spec in specs}
    assert {
        "wake-openwakeword",
        "speaker-campp",
        "embed-granite-r2",
        "face-sface",
        "vad-silero",
        "liveness-audio",
        "scene-yamnet",
    } <= set(by_name)
    # Local LLM / legacy / trainer entries are optional and never expected.
    for name in ("llm-mlx-3b", "qwen3-1.7b", "trainer-mlx-lora", "embed-all-minilm-l6-v2"):
        assert by_name[name].optional, name


def test_kokoro_voice_entries_are_pinned_and_targeted() -> None:
    by_name = {spec.name: spec for spec in builtin_models()}
    model = by_name["tts-kokoro-82m-int8"]
    voices = by_name["tts-kokoro-voices-v1.0"]
    assert model.verified
    assert model.sha256 and len(model.sha256) == 64
    assert voices.verified
    assert voices.sha256 and len(voices.sha256) == 64
    # The shared voices artifact is engine-neutral (used by every Kokoro
    # precision); the higher-fidelity fp16 engine is registered alongside.
    assert voices.target_name == "tts-kokoro.voices"
    fp16 = by_name["tts-kokoro-82m-fp16"]
    assert fp16.verified
    assert fp16.sha256 and len(fp16.sha256) == 64
    assert "fp16" in fp16.source_url


def test_target_name_overrides_artifact_filename(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    source = tmp_path / "x.bin"
    source.write_bytes(b"x")
    spec = make_spec(
        "voices",
        source_url=source.as_uri(),
        sha256=hashlib.sha256(b"x").hexdigest(),
    ).model_copy(update={"target_name": "tts-kokoro.voices"})
    assert store.target_path(settings, spec).name == "tts-kokoro.voices.bin"
    path = store.download_model(spec, settings)
    assert path.name == "tts-kokoro.voices.bin"


def test_pull_verified_local_file(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    source = tmp_path / "source.bin"
    source.write_bytes(b"model-data" * 4096)
    spec = make_spec("m", source_url=source.as_uri(), sha256=hashlib.sha256(source.read_bytes()).hexdigest())
    path = store.download_model(spec, settings)
    assert path.exists()
    assert path.read_bytes() == source.read_bytes()
    assert not list(settings.ml_model_dir.glob("*.part"))


def test_pull_refuses_unpinned_seed_entry(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    source = tmp_path / "source.bin"
    source.write_bytes(b"seed")
    spec = make_spec("seed", source_url=source.as_uri())
    with pytest.raises(RegistryError, match="no sha256 pin"):
        store.download_model(spec, settings)
    assert not list(settings.ml_model_dir.glob("*"))


def test_download_refused_below_disk_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings(tmp_path, min_free_gb=5.0)
    source = tmp_path / "source.bin"
    source.write_bytes(b"data")
    spec = make_spec("m", source_url=source.as_uri(), sha256=hashlib.sha256(b"data").hexdigest())
    monkeypatch.setattr(store, "disk_free_gb", lambda path: 1.0)
    with pytest.raises(DiskGuardError, match="refusing download"):
        store.download_model(spec, settings)
    assert not list(settings.ml_model_dir.glob("*"))


def test_corrupt_checksum_rejected_and_file_removed(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    source = tmp_path / "source.bin"
    source.write_bytes(b"corrupt-me")
    spec = make_spec("m", source_url=source.as_uri(), sha256="0" * 64)
    with pytest.raises(ChecksumError, match="sha256 mismatch"):
        store.download_model(spec, settings)
    assert not list(settings.ml_model_dir.glob("*"))


def test_pull_resumes_from_partial_file(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    payload = b"0123456789" * 1024
    source = tmp_path / "source.bin"
    source.write_bytes(payload)
    spec = make_spec("m", source_url=source.as_uri(), sha256=hashlib.sha256(payload).hexdigest())
    target = store.target_path(settings, spec)
    half = len(payload) // 2
    target.with_name(f"{target.name}.part").write_bytes(payload[:half])
    path = store.download_model(spec, settings)
    assert path.read_bytes() == payload
    assert not list(settings.ml_model_dir.glob("*.part"))


def test_verify_removes_corrupt_cache(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    spec = make_spec("m", source_url=(tmp_path / "x.bin").as_uri(), sha256="1" * 64)
    target = store.target_path(settings, spec)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"bad")
    with pytest.raises(ChecksumError, match="corrupt"):
        store.verify_model(spec, settings)
    assert not target.exists()


def test_prune_evicts_least_recently_used_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path, min_free_gb=5.0)
    cache = settings.ml_model_dir
    cache.mkdir(parents=True)
    old = cache / "old.bin"
    new = cache / "new.bin"
    old.write_bytes(b"old")
    new.write_bytes(b"new")
    base = old.stat().st_mtime
    os.utime(old, (base - 100, base - 100))
    os.utime(new, (base, base))
    monkeypatch.setattr(store, "disk_free_gb", lambda path: 1.0)
    removed = store.prune_models(settings)
    assert removed == [old, new]


def test_ml_doctor_prints_required_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("EV_ML_MODEL_DIR", str(tmp_path / "models"))
    get_ml_settings.cache_clear()
    try:
        assert cli.main(["doctor"]) == 0
    finally:
        get_ml_settings.cache_clear()
    out = capsys.readouterr().out
    assert "posture:" in out
    assert "api_first_pinned_mb: 43" in out
    for name in ("wake-openwakeword", "speaker-campp", "embed-granite-r2", "face-sface"):
        assert f"{name}:" in out
    assert "backend:" in out
    assert "ceiling_mb:" in out
    assert "resident_total_mb:" in out
    assert "free_disk_gb:" in out
