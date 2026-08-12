"""Hard-budget model arbiter for the EV ML runtime.

Rules (see docs/MODEL_BUDGET.md):
* ``always``/``system`` models pin at boot and are never silently evicted.
* ``on_demand`` models share one LRU-evicted slot (default 600 MB).
* ``exclusive`` models evict every on-demand model and take a global lock.
* Any load that would breach ``EV_ML_RESIDENT_CEILING_MB`` is refused with a
  clear error; nothing is silently swapped to make room.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from app.ml.device import select_backend
from app.ml.registry import ModelRegistry, ModelSpec, ModelTier, builtin_models
from app.ml.settings import MLSettings, get_ml_settings
from app.ml.store import disk_free_gb


class ModelLoadRefused(RuntimeError):
    """A load was refused because it would breach a hard budget rule."""


class ModelBusyError(RuntimeError):
    """A model is in use and cannot be evicted or replaced."""


class ModelEvictionRefused(RuntimeError):
    """Eviction was refused (active use or pinned tier)."""


class ModelNotLoadedError(KeyError):
    """Requested eviction target is not resident."""


@dataclass
class _Resident:
    spec: ModelSpec
    pinned: bool
    active: int = 0
    loaded_at: float = field(default_factory=time.monotonic)
    last_used: float = field(default_factory=time.monotonic)


class ModelArbiter:
    """Memory-policy controller for registered models."""

    def __init__(self, registry: ModelRegistry, settings: MLSettings | None = None) -> None:
        self.registry = registry
        self.settings = settings or get_ml_settings()
        self._lock = threading.RLock()
        self._residents: dict[str, _Resident] = {}
        self._exclusive: str | None = None
        self._refusals: list[dict] = []

    # -- boot ----------------------------------------------------------------

    def pin_always(self) -> list[ModelSpec]:
        """Pin expected ``always``/``system`` models; optional ones are skipped."""

        with self._lock:
            pinned: list[ModelSpec] = []
            for tier in (ModelTier.ALWAYS, ModelTier.SYSTEM):
                for spec in self.registry.by_tier(tier):
                    if spec.optional:
                        continue
                    self._load_locked(spec, pin=True)
                    pinned.append(spec)
            return pinned

    # -- acquire -------------------------------------------------------------

    @contextmanager
    def acquire(
        self,
        name: str,
        *,
        release_on_exit: bool = False,
    ) -> Iterator[ModelSpec]:
        """Reserve resident memory for ``name`` and yield its spec.

        ``release_on_exit=True`` evicts an exclusive model when the context
        exits, releasing the global lock. On-demand and pinned models stay
        resident for reuse.
        """

        spec = self.registry.get(name)
        with self._lock:
            resident = self._residents.get(name)
            if resident is None:
                self._load_locked(spec)
                resident = self._residents[name]
            resident.active += 1
        try:
            yield spec
        finally:
            with self._lock:
                resident.active = max(0, resident.active - 1)
                resident.last_used = time.monotonic()
                if release_on_exit and spec.tier is ModelTier.EXCLUSIVE:
                    self._evict_locked(name)

    # -- eviction ------------------------------------------------------------

    def evict(self, name: str, *, force: bool = False) -> None:
        with self._lock:
            self._evict_locked(name, force=force)

    def evict_all(self, tier: ModelTier | None = None) -> int:
        with self._lock:
            if tier is not None:
                targets = [
                    name for name, resident in self._residents.items() if resident.spec.tier is tier
                ]
            else:
                targets = list(self._residents)
            for name in targets:
                self._evict_locked(name, force=True)
            return len(targets)

    def is_resident(self, name: str) -> bool:
        with self._lock:
            return name in self._residents

    def active_names(self) -> list[str]:
        with self._lock:
            return [name for name, resident in self._residents.items() if resident.active > 0]

    # -- stats ---------------------------------------------------------------

    def stats(self) -> dict:
        with self._lock:
            models = [
                {
                    "name": name,
                    "tier": resident.spec.tier.value,
                    "resident_mb": resident.spec.resident_mb,
                    "active": resident.active,
                    "pinned": resident.pinned,
                    "last_used": round(resident.last_used, 3),
                }
                for name, resident in sorted(self._residents.items())
            ]
            by_tier: dict[str, int] = {}
            for resident in self._residents.values():
                key = resident.spec.tier.value
                by_tier[key] = by_tier.get(key, 0) + resident.spec.resident_mb
            backend = select_backend()
            return {
                "ceiling_mb": self.settings.ml_resident_ceiling_mb,
                "resident_total_mb": sum(
                    resident.spec.resident_mb for resident in self._residents.values()
                ),
                "resident_by_tier_mb": by_tier,
                "on_demand_slot_mb": self.settings.ml_on_demand_slot_mb,
                "exclusive_holder": self._exclusive,
                "models": models,
                "backend": backend["backend"],
                "backend_reason": backend["reason"],
                "free_disk_gb": round(disk_free_gb(self.settings.ml_model_dir), 2),
                "refusals_last_50": list(self._refusals[-50:]),
            }

    # -- internals -----------------------------------------------------------

    def _load_locked(self, spec: ModelSpec, *, pin: bool = False) -> _Resident:
        existing = self._residents.get(spec.name)
        if existing is not None:
            if pin:
                existing.pinned = True
            return existing

        if spec.tier is ModelTier.EXCLUSIVE:
            if self._exclusive is not None and self._exclusive != spec.name:
                self._note_refusal(
                    spec,
                    f"exclusive model {self._exclusive!r} holds the global lock",
                )
                raise ModelLoadRefused(
                    f"cannot load {spec.name!r}: exclusive model {self._exclusive!r} "
                    "holds the global lock"
                )
            self._evict_tier_locked(ModelTier.ON_DEMAND)
        elif spec.tier is ModelTier.ON_DEMAND:
            if self._exclusive is not None:
                self._note_refusal(spec, f"exclusive model {self._exclusive!r} holds the global lock")
                raise ModelLoadRefused(
                    f"cannot load on-demand model {spec.name!r}: exclusive model "
                    f"{self._exclusive!r} holds the global lock"
                )
            self._evict_lru_on_demand_locked(spec.resident_mb)

        total = sum(resident.spec.resident_mb for resident in self._residents.values())
        if total + spec.resident_mb > self.settings.ml_resident_ceiling_mb:
            self._note_refusal(
                spec,
                f"resident {total}MB + {spec.resident_mb}MB would exceed ceiling "
                f"{self.settings.ml_resident_ceiling_mb}MB",
            )
            raise ModelLoadRefused(
                f"refusing to load {spec.name!r}: resident {total}MB + "
                f"{spec.resident_mb}MB would exceed ceiling "
                f"{self.settings.ml_resident_ceiling_mb}MB; nothing was evicted"
            )

        resident = _Resident(
            spec=spec,
            pinned=pin or spec.tier in (ModelTier.ALWAYS, ModelTier.SYSTEM),
        )
        self._residents[spec.name] = resident
        if spec.tier is ModelTier.EXCLUSIVE:
            self._exclusive = spec.name
        return resident

    def _evict_lru_on_demand_locked(self, needed_mb: int) -> None:
        while True:
            total = sum(
                resident.spec.resident_mb
                for resident in self._residents.values()
                if resident.spec.tier is ModelTier.ON_DEMAND
            )
            if total + needed_mb <= self.settings.ml_on_demand_slot_mb:
                return
            victims = [
                resident
                for resident in self._residents.values()
                if resident.spec.tier is ModelTier.ON_DEMAND and resident.active == 0
            ]
            if not victims:
                self._note_refusal(
                    None,
                    f"on-demand slot {self.settings.ml_on_demand_slot_mb}MB cannot hold "
                    f"{needed_mb}MB alongside active models",
                )
                raise ModelLoadRefused(
                    f"on-demand slot {self.settings.ml_on_demand_slot_mb}MB cannot hold "
                    f"{needed_mb}MB alongside active models"
                )
            victim = min(victims, key=lambda resident: resident.last_used)
            self._residents.pop(victim.spec.name, None)

    def _evict_tier_locked(self, tier: ModelTier) -> None:
        active = [
            resident for resident in self._residents.values() if resident.spec.tier is tier and resident.active > 0
        ]
        if active:
            names = ", ".join(sorted(resident.spec.name for resident in active))
            raise ModelBusyError(f"cannot evict {tier.value} models still in use: {names}")
        for name, resident in list(self._residents.items()):
            if resident.spec.tier is tier:
                self._residents.pop(name, None)

    def _evict_locked(self, name: str, *, force: bool = False) -> None:
        resident = self._residents.get(name)
        if resident is None:
            raise ModelNotLoadedError(f"model {name!r} is not resident")
        if resident.active > 0:
            raise ModelEvictionRefused(f"model {name!r} is in use; cannot evict")
        if resident.pinned and not force:
            raise ModelEvictionRefused(
                f"model {name!r} is pinned ({resident.spec.tier.value}); pass force=True"
            )
        self._residents.pop(name, None)
        if self._exclusive == name:
            self._exclusive = None

    def _note_refusal(self, spec: ModelSpec | None, reason: str) -> None:
        self._refusals.append(
            {
                "at": round(time.monotonic(), 3),
                "model": spec.name if spec else None,
                "reason": reason,
            }
        )


def create_default_arbiter(settings: MLSettings | None = None) -> ModelArbiter:
    """Registry + arbiter with the locked built-in roster."""

    settings = settings or get_ml_settings()
    registry = ModelRegistry(exclusive_limit_mb=settings.ml_exclusive_limit_mb)
    for spec in builtin_models():
        registry.register(spec)
    return ModelArbiter(registry, settings)
