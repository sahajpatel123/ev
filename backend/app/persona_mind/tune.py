"""Versioned tunables (M2 table + M3 writer). Dreams propose, canary gates.

Table `config`: every applied tuning is a new immutable version row with a
diff summary. `active_version` in meta selects. Rollback = point at an
earlier version (M3 ships the CLI).
"""

from __future__ import annotations

from typing import Any

from app.persona_mind import store

DEFAULT_TUNABLES: dict[str, Any] = {
    # Pace rules (room.py).
    "blitz_len_z": -1.0,
    "blitz_gap_z": -1.0,
    "deep_len_z": 1.5,
    "deep_min_chars": 200,
    "urgency_min": 2,
    "depth_min": 1,
    "burst_n": 3,
    "burst_window_s": 120,
    "stuck_fails": 1,
    "late_hour_start": 23,
    "early_hour_end": 6,
    "typo_z": 2.0,
    # Learning priors (persona weights live in CLI 6's lane; the mind keeps
    # only its own joke-rate prior for the ROOM line).
    "joke_rate_prior": 0.5,
}

# Bounds the canary/dream path must respect. Anything outside is rejected.
TUNABLE_BOUNDS: dict[str, tuple[float, float]] = {
    "blitz_len_z": (-3.0, 0.0),
    "blitz_gap_z": (-3.0, 0.0),
    "deep_len_z": (0.5, 3.0),
    "deep_min_chars": (50.0, 1000.0),
    "urgency_min": (1.0, 5.0),
    "depth_min": (1.0, 5.0),
    "burst_n": (2.0, 10.0),
    "burst_window_s": (30.0, 600.0),
    "stuck_fails": (1.0, 5.0),
    "late_hour_start": (20.0, 23.0),
    "early_hour_end": (4.0, 9.0),
    "typo_z": (1.0, 4.0),
    "joke_rate_prior": (0.15, 0.7),
}


def ensure_v1(*, path: Any = None) -> int:
    """Create version 1 (defaults) when no config exists. Returns active."""
    active = store.meta_get("active_config_version", "", path=path)
    if active:
        return int(active)
    version = store.put_config(
        {"tunables": dict(DEFAULT_TUNABLES), "lessons": [], "exemplars": []},
        diff="v1 defaults",
        path=path,
    )
    store.meta_set("active_config_version", str(version), path=path)
    return version


def active(*, path: Any = None) -> dict[str, Any]:
    """Active config {tunables, lessons, exemplars, version}."""
    version = ensure_v1(path=path)
    row = store.get_config(version, path=path)
    data = dict(row["data"]) if row else {"tunables": {}, "lessons": [], "exemplars": []}
    data["version"] = version
    return data


def tunables(*, path: Any = None) -> dict[str, Any]:
    """Active tunables merged over defaults (unknown keys dropped)."""
    merged = dict(DEFAULT_TUNABLES)
    for key, value in (active(path=path).get("tunables") or {}).items():
        if key in merged:
            try:
                merged[key] = float(value)
            except (TypeError, ValueError):
                continue
    return merged


def validate_tunable_updates(updates: dict[str, Any]) -> tuple[dict[str, float], list[str]]:
    """Clamp-check dream-proposed tunable updates. Returns (valid, rejected)."""
    valid: dict[str, float] = {}
    rejected: list[str] = []
    for key, value in (updates or {}).items():
        if key not in TUNABLE_BOUNDS:
            rejected.append(f"{key}: unknown tunable")
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            rejected.append(f"{key}: not a number")
            continue
        low, high = TUNABLE_BOUNDS[key]
        if not low <= number <= high:
            rejected.append(f"{key}: {number} outside [{low}, {high}]")
            continue
        valid[key] = number
    return valid, rejected


def apply_version(
    *,
    tunable_updates: dict[str, Any] | None = None,
    lessons: list[dict[str, Any]] | None = None,
    exemplars: list[dict[str, Any]] | None = None,
    diff: str = "",
    path: Any = None,
) -> int:
    """Write a new version (caller ran the canary gate first). Returns it."""
    current = active(path=path)
    tunables_next = dict(current.get("tunables") or {})
    tunables_next.update(tunable_updates or {})
    version = store.put_config(
        {
            "tunables": tunables_next,
            "lessons": lessons if lessons is not None else list(current.get("lessons") or []),
            "exemplars": exemplars if exemplars is not None else list(current.get("exemplars") or []),
        },
        diff=diff or "update",
        path=path,
    )
    store.meta_set("active_config_version", str(version), path=path)
    return version


def rollback_to(version: int, *, path: Any = None) -> bool:
    """Point the active version at an earlier one. False when unknown."""
    if store.get_config(version, path=path) is None:
        return False
    store.meta_set("active_config_version", str(version), path=path)
    return True


def history(*, path: Any = None) -> list[dict[str, Any]]:
    return store.list_configs(path=path)
