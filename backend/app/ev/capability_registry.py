"""Runtime capability projectors for the Live Capability Manifest.

Tool specs remain the declaration source. Projectors bind those tools to live
device and permission state so the manifest is derived, not hand-written.
Camera and computer register themselves; future features should too.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

OverlayFn = Callable[[dict[str, Any], Any], dict[str, Any]]


@dataclass(frozen=True)
class RegisteredCapability:
    name: str
    description: str
    tools: frozenset[str]
    overlay: OverlayFn
    readiness_key: str
    risk_class: str = "R1"


_REGISTRY: dict[str, RegisteredCapability] = {}


def register_capability(capability: RegisteredCapability) -> RegisteredCapability:
    _REGISTRY[capability.name] = capability
    return capability


def registered_capabilities() -> list[RegisteredCapability]:
    return list(_REGISTRY.values())


def apply_capability_overlays(
    entries: list[dict[str, Any]],
    readiness: dict[str, Any],
) -> list[dict[str, Any]]:
    """Overlay registered tool families onto the current runtime projection."""

    out = list(entries)
    for capability in registered_capabilities():
        state = readiness.get(capability.name)
        if state is None:
            continue
        for index, entry in enumerate(out):
            if str(entry.get("name") or "") in capability.tools:
                out[index] = capability.overlay(dict(entry), state)
    return out
