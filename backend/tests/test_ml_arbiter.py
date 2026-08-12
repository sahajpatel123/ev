"""Model arbiter acceptance: ceiling, LRU slot, exclusive lock, fuzz."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from app.ml.arbiter import ModelArbiter, ModelEvictionRefused, ModelLoadRefused
from app.ml.registry import ModelRegistry, ModelSpec, ModelTier
from app.ml.settings import MLSettings


def make_settings(
    tmp_path: Path, *, ceiling_mb: int = 300, slot_mb: int = 200
) -> MLSettings:
    return MLSettings(
        _env_file=None,
        ml_model_dir=tmp_path / "models",
        ml_dataset_dir=tmp_path / "datasets",
        ml_resident_ceiling_mb=ceiling_mb,
        ml_on_demand_slot_mb=slot_mb,
        ml_min_free_gb=0.0,
        ml_exclusive_limit_mb=3500,
    )


def spec(
    name: str,
    tier: ModelTier,
    resident_mb: int,
    *,
    optional: bool = False,
) -> ModelSpec:
    return ModelSpec(
        name=name,
        task="test",
        disk_mb=resident_mb,
        resident_mb=resident_mb,
        peak_mb=resident_mb,
        tier=tier,
        license="MIT",
        optional=optional,
    )


@pytest.fixture
def arb(tmp_path: Path) -> ModelArbiter:
    reg = ModelRegistry()
    reg.register(spec("always", ModelTier.ALWAYS, 20))
    reg.register(spec("system", ModelTier.SYSTEM, 10))
    reg.register(spec("od-0", ModelTier.ON_DEMAND, 50))
    reg.register(spec("od-1", ModelTier.ON_DEMAND, 80))
    reg.register(spec("od-2", ModelTier.ON_DEMAND, 110))
    reg.register(spec("excl", ModelTier.EXCLUSIVE, 250))
    reg.register(spec("huge", ModelTier.EXCLUSIVE, 500))
    return ModelArbiter(reg, make_settings(tmp_path))


def test_pin_always_reserves_budget(arb: ModelArbiter) -> None:
    pinned = arb.pin_always()
    assert {item.name for item in pinned} == {"always", "system"}
    stats = arb.stats()
    assert stats["resident_total_mb"] == 30
    assert stats["resident_by_tier_mb"] == {"always": 20, "system": 10}
    assert all(item["pinned"] for item in stats["models"])


def test_pin_always_skips_optional_models(arb: ModelArbiter) -> None:
    arb.registry.register(spec("opt-always", ModelTier.ALWAYS, 5, optional=True))
    pinned = arb.pin_always()
    assert "opt-always" not in {item.name for item in pinned}
    assert not arb.is_resident("opt-always")
    # Optional models remain loadable on request.
    with arb.acquire("opt-always"):
        assert arb.is_resident("opt-always")
    assert arb.stats()["resident_total_mb"] == 30 + 5


def test_ceiling_breach_refused_and_nothing_evicted(arb: ModelArbiter) -> None:
    arb.pin_always()
    arb.registry.register(spec("too-big", ModelTier.EXCLUSIVE, 280))
    with pytest.raises(ModelLoadRefused, match="ceiling"), arb.acquire("too-big"):
        pass
    assert arb.stats()["resident_total_mb"] == 30
    assert arb.stats()["refusals_last_50"]


def test_on_demand_slot_uses_lru_eviction(arb: ModelArbiter) -> None:
    arb.pin_always()
    with arb.acquire("od-0"):
        pass
    with arb.acquire("od-1"):
        pass
    with arb.acquire("od-2"):
        pass
    assert not arb.is_resident("od-0")
    assert arb.is_resident("od-1")
    assert arb.is_resident("od-2")
    assert arb.stats()["resident_total_mb"] == 30 + 80 + 110


def test_on_demand_slot_refuses_when_active_models_block(arb: ModelArbiter) -> None:
    arb.pin_always()
    # 50 + 110 = 160 resident; adding od-1 (80) needs 240 > 200 and both
    # residents are active, so nothing can be evicted.
    with (
        arb.acquire("od-0"),
        arb.acquire("od-2"),
        pytest.raises(ModelLoadRefused, match="on-demand slot"),
        arb.acquire("od-1"),
    ):
        pass


def test_exclusive_evicts_on_demand_and_takes_global_lock(arb: ModelArbiter) -> None:
    arb.pin_always()
    with arb.acquire("od-0"), arb.acquire("od-1"):
        pass
    with arb.acquire("excl"):
        pass
    assert not arb.is_resident("od-0")
    assert not arb.is_resident("od-1")
    assert arb.stats()["exclusive_holder"] == "excl"
    with pytest.raises(ModelLoadRefused, match="global lock"), arb.acquire("od-0"):
        pass
    arb.evict("excl")
    assert arb.stats()["exclusive_holder"] is None
    with arb.acquire("od-0"):
        pass


def test_exclusive_release_on_exit_frees_global_lock(arb: ModelArbiter) -> None:
    arb.pin_always()
    with arb.acquire("excl", release_on_exit=True):
        assert arb.stats()["exclusive_holder"] == "excl"
    assert not arb.is_resident("excl")
    assert arb.stats()["exclusive_holder"] is None
    with arb.acquire("od-0"):
        pass


def test_evict_refuses_active_and_pinned(arb: ModelArbiter) -> None:
    arb.pin_always()
    with arb.acquire("od-0"), pytest.raises(ModelEvictionRefused, match="in use"):
        arb.evict("od-0")
    arb.evict("od-0")
    with pytest.raises(ModelEvictionRefused, match="pinned"):
        arb.evict("always")
    arb.evict("always", force=True)
    assert not arb.is_resident("always")


def test_fuzz_1000_loads_never_breach_ceiling(arb: ModelArbiter) -> None:
    arb.pin_always()
    names = ["od-0", "od-1", "od-2", "excl", "huge"]
    rng = random.Random(7)
    refused = 0
    for _ in range(1000):
        name = rng.choice(names)
        tier = arb.registry.get(name).tier
        try:
            with arb.acquire(name, release_on_exit=tier is ModelTier.EXCLUSIVE):
                assert arb.stats()["resident_total_mb"] <= 300
        except ModelLoadRefused:
            refused += 1
        assert arb.stats()["resident_total_mb"] <= 300
    assert refused > 0
    assert arb.stats()["resident_total_mb"] <= 300


def test_stats_shape(arb: ModelArbiter) -> None:
    stats = arb.stats()
    for key in (
        "ceiling_mb",
        "resident_total_mb",
        "resident_by_tier_mb",
        "on_demand_slot_mb",
        "exclusive_holder",
        "models",
        "backend",
        "free_disk_gb",
    ):
        assert key in stats
