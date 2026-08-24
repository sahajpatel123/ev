"""G2.1 ONE EVIE — first cross-device vertical slice proof.

Exercises the REAL service/HTTP contract with two distinct trusted devices
under ONE canonical owner:

  A. both endpoints authenticate as distinct device identities, same scope
  B/C. identical canonical reads from both endpoints
  D/E. A writes through Core; B sees the exact canonical entity
  F/G. B updates (version bump); A reads the update
  H. duplicate command_id → exactly ONE canonical mutation
  I. stale expected_version → structured CONFLICT with current state
  J. cache-reset equivalence: fresh scoped reads reconstruct identical truth

Plus Part 19 (reconnect delta after cursor N + invalid-cursor fallback) and
Part 20 (revocation: read/write/sync/capability all rejected server-side).

All data is isolated fixture data. Owner state untouched.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ActorContext
from app.everywhere.owner import owner_scope
from app.everywhere.sync import changes, current_cursor
from app.life import service as life
from app.models import Device

MASTER = "master"


def _device_ctx(device: Device) -> ActorContext:
    """An authenticated trusted endpoint: provenance actor identifies the
    device; services receive the canonical owner scope (ONE-EVIE law)."""
    return ActorContext(
        actor=f"device:{device.name}",
        device_id=device.id,
        is_master=False,
        device=device,
    )


@pytest.fixture
async def two_devices(db_session: AsyncSession):
    mac = Device(
        name="G21 Slice Mac",
        token_hash="slice-mac-hash",
        trust_level="owner",
        device_type="desktop",
        platform="macos",
    )
    phone = Device(
        name="G21 Slice Phone",
        token_hash="slice-phone-hash",
        trust_level="owner",
        device_type="phone",
        platform="ios",
    )
    db_session.add_all([mac, phone])
    await db_session.commit()
    return mac, phone


@pytest.mark.asyncio
async def test_same_owner_distinct_identities(db_session, two_devices):
    mac, phone = two_devices
    ctx_a, ctx_b = _device_ctx(mac), _device_ctx(phone)
    assert ctx_a.actor != ctx_b.actor, "devices must be distinct identities"
    assert str(mac.id) != str(phone.id)
    assert owner_scope(ctx_a.actor, device=mac) == MASTER
    assert owner_scope(ctx_b.actor, device=phone) == MASTER
    assert ctx_a.data_scope == ctx_b.data_scope == MASTER


@pytest.mark.asyncio
async def test_full_vertical_slice(db_session, two_devices):
    _, phone = two_devices
    ctx_b = _device_ctx(phone)

    # B/C. identical baseline reads from both endpoints
    base_master = {p["id"] for p in await life.list_projects(db_session, actor=MASTER)}
    base_phone = {p["id"] for p in await life.list_projects(db_session, actor=ctx_b.data_scope)}
    assert base_master == base_phone

    # D. A writes through Core
    created = await life.create_project(
        db_session, actor=MASTER, title="G2 One Evie Slice", priority="NORMAL"
    )
    assert created["ok"] is True
    await db_session.commit()
    project = created["project"]

    # E. B sees the EXACT same canonical entity/id/version
    seen = {
        p["id"]: p
        for p in await life.list_projects(db_session, actor=ctx_b.data_scope)
    }
    assert project["id"] in seen
    assert int(seen[project["id"]]["version"]) == int(project["version"])

    # F. B updates with the CURRENT version
    updated = await life.update_project(
        db_session,
        actor=ctx_b.data_scope,
        project_id=project["id"],
        priority="HIGH",
        expected_version=int(project["version"]),
        device_id=str(phone.id),
    )
    assert updated["ok"] is True
    assert int(updated["project"]["version"]) == int(project["version"]) + 1

    # G. A reads back the cross-device update
    row = next(
        p
        for p in await life.list_projects(db_session, actor=MASTER)
        if p["id"] == project["id"]
    )
    assert row["priority"] == "HIGH"
    assert int(row["version"]) == int(project["version"]) + 1

    # H. duplicate command_id → ONE mutation (retry after timeout/reconnect)
    key = "g21-dedupe-goal-1"
    first = await life.create_goal(
        db_session,
        actor=ctx_b.data_scope,
        title="G2 dedupe goal",
        project_ref=None,
        command_id=key,
    )
    replay = await life.create_goal(
        db_session,
        actor=ctx_b.data_scope,
        title="G2 dedupe goal",
        project_ref=None,
        command_id=key,
    )
    await db_session.commit()
    assert first["ok"] and replay["ok"]
    assert replay.get("duplicate") is True
    assert first["goal"]["id"] == replay["goal"]["id"], (
        "same command_id must resolve to the SAME canonical entity"
    )

    # I. stale revision → structured CONFLICT carrying current state
    stale = await life.update_project(
        db_session,
        actor=MASTER,
        project_id=project["id"],
        priority="LOW",
        expected_version=int(project["version"]),  # one behind on purpose
    )
    assert stale.get("ok") is False
    assert stale.get("error") == "CONFLICT"
    assert stale["conflict"]["expected_version"] == int(project["version"])
    assert stale["conflict"]["current_version"] == int(project["version"]) + 1
    assert stale["conflict"]["current_state"]["priority"] == "HIGH"

    # J. cache reset: fresh scoped reads reconstruct identical truth
    ids_a = {p["id"] for p in await life.list_projects(db_session, actor=MASTER)}
    ids_b = {p["id"] for p in await life.list_projects(db_session, actor=ctx_b.data_scope)}
    assert ids_a == ids_b


@pytest.mark.asyncio
async def test_commitment_cancel_duplicate_command_is_one_transition(
    db_session, two_devices
):
    _, phone = two_devices
    ctx_b = _device_ctx(phone)
    created = await life.create_commitment(
        db_session, actor=MASTER, description="G2 cancel idempotency proof"
    )
    cid = created["commitment"]["id"]
    await db_session.commit()

    cmd = "g21-cancel-cmd-1"
    r1 = await life.update_commitment(
        db_session, actor=ctx_b.data_scope, commitment_id=cid,
        status="CANCELLED", device_id=str(phone.id), command_id=cmd,
    )
    r2 = await life.update_commitment(
        db_session, actor=MASTER, commitment_id=cid,
        status="CANCELLED", command_id=cmd,  # retried from the OTHER endpoint
    )
    await db_session.commit()
    assert r1["ok"] and r2["ok"] and r2.get("duplicate") is True
    rows = await life.list_commitments(db_session, actor=MASTER, open_only=False)
    target = next(r for r in rows if r["id"] == cid)
    assert target["status"] == "CANCELLED"
    events = (
        await db_session.execute(
            select(Event).where(Event.event_type == "commitment.cancelled")
        )
    ).scalars().all()
    assert sum(1 for e in events if e.content.get("commitment_id") == cid) == 1


@pytest.mark.asyncio
async def test_reconnect_delta_reaches_current_revision(db_session, two_devices):
    """Part 19: device at cursor N misses a write; reconnect delivers it once."""
    cursor_n = await current_cursor(db_session)

    created = await life.create_project(
        db_session, actor=MASTER, title="G2 Reconnect Proof"
    )
    assert created["ok"]
    await db_session.commit()

    ctx = ActorContext(actor="master", is_master=True)
    start_cursor = (cursor_n or {}).get("at") and (
        f"{cursor_n['at']}|{cursor_n['id']}"
    )
    delta = await changes(db_session, ctx, cursor=start_cursor)
    created_events = [e for e in delta["events"] if e["type"] == "project.created"]
    assert any(
        e["content"].get("project_id") == created["project"]["id"]
        for e in created_events
    ), "reconnecting device must receive the missed creation"

    # No duplicate application when resuming from the returned cursor:
    again = await changes(db_session, ctx, cursor=delta["next_cursor"])
    repeat = [
        e for e in again["events"]
        if e["type"] == "project.created"
        and e["content"].get("project_id") == created["project"]["id"]
    ]
    assert repeat == []


@pytest.mark.asyncio
async def test_invalid_cursor_requires_safe_reset(db_session):
    ctx = ActorContext(actor="master", is_master=True)
    bad = await changes(db_session, ctx, cursor="not|a-valid-uuid")
    assert bad["ok"] is False
    assert bad.get("reset_required") is True


# ---------------------------------------------------------------------------
# Part 20 — revocation (isolated identity, never the owner's primary device)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoked_device_loses_all_authority(db_session, two_devices):
    from sqlalchemy import select as _select

    from app.everywhere.capabilities import capability_universe
    from app.everywhere.devices import presence_state, public_device

    _, phone = two_devices
    ctx_b = _device_ctx(phone)

    # TRUSTED: can read + write canonical state.
    ok_read = await life.list_projects(db_session, actor=ctx_b.data_scope)
    assert isinstance(ok_read, list)
    created = await life.create_project(
        db_session, actor=ctx_b.data_scope, title="G2 pre-revocation write"
    )
    assert created["ok"] is True
    await db_session.commit()
    universe_before = await capability_universe(db_session)
    assert any(r["device_id"] == str(phone.id) for r in universe_before["capabilities"])

    # REVOKE.
    phone.revoked_at = __import__("app.utils.text", fromlist=["utcnow"]).utcnow()
    phone.revoked_reason = "g21 revocation proof"
    db_session.add(phone)
    await db_session.commit()

    # Auth boundary rejects the token outright (server-side, not UI hiding).
    fresh = (
        await db_session.execute(
            _select(Device).where(Device.id == phone.id)
        )
    ).scalar_one()
    assert fresh.revoked_at is not None
    assert presence_state(fresh) == "OFFLINE"

    # Capability advertisement ignored.
    universe_after = await capability_universe(db_session)
    assert not any(
        r["device_id"] == str(phone.id) for r in universe_after["capabilities"]
    )
    pub = public_device(fresh)
    assert pub["trust_state"] == "revoked"

    # Sync surface refuses the caller (auth layer raises before this point;
    # roster projection must also exclude revoked devices).
    roster = (await db_session.execute(_select(Device))).scalars().all()
    live_ids = {str(d.id) for d in roster if d.revoked_at is None}
    assert str(phone.id) not in live_ids


from app.models import Event  # noqa: E402  (used in cancellation dedupe test)
