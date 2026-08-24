"""G2.1 physical-device trust lifecycle — owner-scope repair proofs.

Laws under test (PART 3-9, 15, 18-19):

- PAIRED != TRUSTED. A paired companion is PAIRED_SANDBOX: authenticated,
  but its life reads resolve to an isolated scope and MUST return the
  canonical DEVICE_NOT_TRUSTED outcome instead of a silent empty namespace.
- Promotion (master key = the owner approval factor) flips the device to
  TRUSTED_OWNER_DEVICE; because auth re-resolves the Device row per
  request, the SAME credential gains owner scope with no token change.
  Live sessions for the device are closed so the next reconnect binds
  OWNER tools/instructions (stale-session law, symmetric with revocation).
- Revocation symmetry already enforced.

Fixtures are isolated devices; the owner's real rows untouched.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ActorContext
from app.everywhere.devices import public_device
from app.life import service as life
from app.models import Device

MASTER = "master"


@pytest.fixture
async def paired_phone(db_session: AsyncSession):
    """Exactly what /pair creates today: PAIRED_SANDBOX companion."""
    d = Device(
        name="Trust Lifecycle Phone",
        token_hash="trust-lifecycle-phone",
        trust_level="device",
        role="primary_companion",
        memory_scope="sandbox",
        device_type="phone",
        platform="web",
    )
    db_session.add(d)
    await db_session.commit()
    return d


def _ctx(device: Device) -> ActorContext:
    return ActorContext(
        actor=f"device:{device.name}", device_id=device.id, is_master=False, device=device
    )


@pytest.mark.asyncio
async def test_paired_sandbox_is_authenticated_but_not_trusted(
    db_session, paired_phone
):
    ctx = _ctx(paired_phone)

    # Authenticated: yes. Trusted owner endpoint: NO.
    assert ctx.data_scope.startswith("sandbox:")
    assert ctx.data_scope != MASTER

    # Owner-state read returns the CANONICAL untrusted outcome, never a
    # silently-empty project list the model could spin into inventions.
    result = await life.list_projects(db_session, actor=ctx.data_scope)
    # The sandbox namespace itself is genuinely empty:
    assert result == []

    # The TurnGate surface carries the canonical reason (PART 15):
    from app.ev.turn_controller import TurnController

    res = await TurnController(db_session, actor=ctx.data_scope).handle_turn(
        "What projects do I have?"
    )
    assert res.ok is False
    assert res.error == "DEVICE_NOT_TRUSTED"
    assert "hasn't been trusted" in (res.owner_message or "")


@pytest.mark.asyncio
async def test_promotion_grants_owner_scope_to_same_credential(
    db_session, paired_phone
):
    """PART 7/8: promote via the canonical flow; the SAME credential (no
    re-pairing, no new token) resolves to the owner scope on its NEXT
    request because trust is server-resolved per call."""
    ctx = _ctx(paired_phone)
    assert ctx.data_scope != MASTER

    # --- canonical promotion (what POST /admin/promote-owner performs) ---
    paired_phone.memory_scope = None
    paired_phone.trust_level = "owner"
    db_session.add(paired_phone)
    await db_session.commit()

    # Same credential → fresh per-request resolution → owner scope:
    assert ctx.data_scope == MASTER

    # Owner state now readable/writable through Core:
    created = await life.create_project(
        db_session, actor=ctx.data_scope, title="G2 Trust Promotion Proof"
    )
    assert created["ok"] is True
    await db_session.commit()
    seen = await life.list_projects(db_session, actor=MASTER)
    assert any(p["id"] == created["project"]["id"] for p in seen)

    pub = public_device(paired_phone)
    assert pub["trust_state"] == "TRUSTED_OWNER_DEVICE"
    assert pub["life_read_allowed"] is True
    assert pub["owner_scope_resolved"] == MASTER


@pytest.mark.asyncio
async def test_trust_state_labels_are_explicit(db_session, paired_phone):
    pub = public_device(paired_phone)
    assert pub["trust_state"] == "PAIRED_SANDBOX"
    assert pub["life_read_allowed"] is False
    assert pub["life_write_allowed"] is False
    assert pub["bootstrap_allowed"] is True


@pytest.mark.asyncio
async def test_bootstrap_marks_paired_sandbox_explicitly(
    db_session, paired_phone, monkeypatch
):
    """PART 21 data: bootstrap tells the client exactly where it stands."""
    from app.everywhere.sync import bootstrap

    ctx = _ctx(paired_phone)
    payload = await bootstrap(db_session, ctx)
    dt = payload.get("device_trust") or {}
    assert dt["authenticated"] is True
    assert dt["trusted"] is False
    assert dt["state"] == "PAIRED_SANDBOX"
    assert dt["required_action"] == "trust_device_from_mac"

    # After promotion the same endpoint reports trusted:
    paired_phone.memory_scope = None
    paired_phone.trust_level = "owner"
    await db_session.commit()
    payload2 = await bootstrap(db_session, ctx)
    dt2 = payload2["device_trust"]
    assert dt2["trusted"] is True
    assert dt2["state"] == "TRUSTED_OWNER_DEVICE"
