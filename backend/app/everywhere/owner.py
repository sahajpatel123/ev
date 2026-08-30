"""G2 Phase 1 — Unified owner identity.

ONE OWNER. Every trusted endpoint resolves to the same canonical owner scope,
so a Project created from any device is visible from every device.

The G1 canonical tables scope rows with an ``actor`` string. Before G2, Mac
(master key) wrote ``actor="master"`` while a device token would have written
``actor="device:<name>"`` — three devices could have produced three partially
independent state silos. This module is the fix at the service boundary: all
life-state services map their incoming actor through :func:`owner_scope`, so
row ownership never depends on which device created it.

Device provenance is preserved separately (Event.device_id / Event content
attribution) — identity of the owner is not identity of the actor device.

Sandbox isolation law: gateway sandbox devices keep writing to their own
isolated scope and can NEVER touch production owner state.
"""

from __future__ import annotations

from app.models import Device

# The single canonical owner scope for v0.1 (single-owner system).
CANONICAL_OWNER = "master"

_SANDBOX_PREFIX = "sandbox:"


def is_sandbox(device: Device | None) -> bool:
    if device is None:
        return False
    return str(getattr(device, "memory_scope", "") or "").lower() == "sandbox"


def owner_scope(actor: str | None = None, *, device: Device | None = None) -> str:
    """Resolve ANY authenticated caller to its canonical data scope.

    - master key or any trusted (non-sandbox) device → CANONICAL_OWNER
    - sandbox devices → isolated per-device scope (never production state)
    """
    if device is not None and is_sandbox(device):
        return f"{_SANDBOX_PREFIX}{device.id}"
    return CANONICAL_OWNER


def provenance_label(actor: str | None, *, device: Device | None = None) -> str:
    """Human/model-meaningful attribution of WHO acted (owner vs which device).

    Kept separate from row ownership: events record this in content.actor so
    'Goal X was completed from your phone' stays possible without ever letting
    device identity fragment owner state.
    """
    if actor:
        return str(actor)[:64]
    if device is not None:
        return f"device:{device.name}"
    return CANONICAL_OWNER
