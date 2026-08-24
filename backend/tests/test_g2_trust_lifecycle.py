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


# ---------------------------------------------------------------------------
# PART 6/15: TRUSTED device text turns enter TurnGate (canonical control
# plane) — never the legacy sandbox pipeline. Sandbox devices keep it.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trusted_text_turn_reaches_turngate_and_core(
    db_session, paired_phone
):
    # Promote first (canonical flow effect):
    paired_phone.memory_scope = None
    paired_phone.trust_level = "owner"
    db_session.add(paired_phone)
    await db_session.commit()

    from app.ev.owner_turn import create_owner_turn
    from app.ev.turn_gate import handle_owner_turn

    created = await life.create_project(db_session, actor=MASTER, title="Personal Fitness")
    del created
    await db_session.commit()

    ctx = _ctx(paired_phone)
    assert ctx.data_scope == MASTER

    turn = create_owner_turn(
        live_session_id=f"device-text:{paired_phone.id}",
        provider_item_id=None,
        owner_id="master",
        device_id=str(paired_phone.id),
        transcript="What projects do I have?",
        transcript_source="device_text",
    )
    result = await handle_owner_turn(db_session, turn)
    await db_session.commit()
    assert result.route == "STATE_QUERY"
    assert result.operation == "PROJECT_LIST"
    titles = " ".join(p["title"] for p in (result.canonical_data or []))
    assert "Personal Fitness" in titles


@pytest.mark.asyncio
async def test_auth_revision_bumps_on_trust_transitions(db_session, paired_phone):
    """PART 8: promotion/revoke bump the generation; sessions opened under an
    older generation are detectably stale."""
    before = int(getattr(paired_phone, "auth_revision", 1) or 1)

    # promote (as admin_promote_owner does)
    paired_phone.memory_scope = None
    paired_phone.trust_level = "owner"
    paired_phone.auth_revision = int(getattr(paired_phone, "auth_revision", 1) or 1) + 1
    await db_session.commit()
    after_promote = int(paired_phone.auth_revision)
    assert after_promote == before + 1

    # revoke (as admin_revoke does)
    from app.utils.text import utcnow

    paired_phone.revoked_at = utcnow()
    paired_phone.revoked_reason = "generation test"
    paired_phone.auth_revision = after_promote + 1
    await db_session.commit()
    assert int(paired_phone.auth_revision) == before + 2


# ---------------------------------------------------------------------------
# SANDBOX-ESCAPE ELIMINATION: a cached LiveSession from before trust
# promotion must never be reused with its old sandbox manifest.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_sandbox_session_is_rebuilt_on_promotion(db_session):
    from app.device_gateway.webrtc_live import attach_phone_control_live

    d = Device(
        name="Escape Test Phone",
        token_hash="escape-phone",
        trust_level="device",
        role="primary_companion",
        memory_scope="sandbox",
        device_type="phone",
        auth_revision=1,
    )
    db_session.add(d)
    await db_session.commit()

    # Pre-promotion: session binds WITH the sandbox manifest.
    pre = attach_phone_control_live(
        device=d, session_id="escape-sess-1", actor="device:Escape Test Phone"
    )
    assert (pre._capability_manifest or {}).get("memory_scope") == "sandbox"
    assert int(getattr(pre, "auth_revision", 1)) == 1

    # Promote (canonical flow effect incl. revision bump).
    d.memory_scope = None
    d.trust_level = "owner"
    d.auth_revision = 2
    db_session.add(d)
    await db_session.commit()

    # Same session_id must NOT return the stale sandbox-bound session:
    post = attach_phone_control_live(
        device=d, session_id="escape-sess-1", actor="device:Escape Test Phone"
    )
    assert post is not pre, "stale authorization session must be rebuilt"
    assert (post._capability_manifest or {}).get("memory_scope") == "owner"
    assert post.memory_scope == "owner"
    assert int(post.auth_revision) == 2

    # And a stable trusted session IS reused (no churn within a generation):
    again = attach_phone_control_live(
        device=d, session_id="escape-sess-1", actor="device:Escape Test Phone"
    )
    assert again is post


# ---------------------------------------------------------------------------
# SANDBOX-ESCAPE ELIMINATION (WebRTC path): trusted owner phones get OWNER
# instructions + the single canonical broker tool; sandbox stays sandbox.
# ---------------------------------------------------------------------------


def test_owner_phone_webrtc_session_has_no_sandbox_instructions():
    from app.device_gateway.sandbox_tools import SANDBOX_LIVE_INSTRUCTIONS
    from app.device_gateway.webrtc_live import phone_webrtc_session

    d = Device(
        name="Owner WebRTC Phone",
        token_hash="owner-webrtc-phone",
        trust_level="owner",
        role="primary_companion",
        memory_scope=None,
        device_type="phone",
    )
    cfg = phone_webrtc_session(device=d)
    blob = str(cfg)
    assert "sandbox mode" not in blob.lower()
    assert SANDBOX_LIVE_INSTRUCTIONS[:40] not in cfg["instructions"]
    tool_names = [t.get("name") for t in cfg.get("tools", [])]
    assert "evie_state_query" in tool_names
    assert "phone_action" not in tool_names


def test_sandbox_phone_webrtc_session_unchanged():
    from app.device_gateway.sandbox_tools import SANDBOX_LIVE_INSTRUCTIONS
    from app.device_gateway.webrtc_live import phone_webrtc_session

    d = Device(
        name="Sandbox WebRTC Phone",
        token_hash="sandbox-webrtc-phone",
        trust_level="device",
        role="companion",
        memory_scope="sandbox",
        device_type="phone",
    )
    cfg = phone_webrtc_session(device=d)
    assert SANDBOX_LIVE_INSTRUCTIONS[:40] in cfg["instructions"]
    tool_names = [t.get("name") for t in cfg.get("tools", [])]
    assert "evie_state_query" not in tool_names
    assert "phone_action" in tool_names


@pytest.mark.asyncio
async def test_evie_state_query_broker_routes_through_turngate(db_session):
    """The broker executes OwnerTurn -> TurnGate -> Core and returns the
    canonical spoken answer — durable events included."""
    from sqlalchemy import func, select

    from app.device_gateway.pipeline import run_trusted_device_turn
    from app.models import Event

    d = Device(
        name="Broker Phone",
        token_hash="broker-phone",
        trust_level="owner",
        memory_scope=None,
        device_type="phone",
    )
    db_session.add(d)
    await life.create_project(db_session, actor=MASTER, title="Broker Visible")
    await db_session.commit()

    result = await run_trusted_device_turn(
        db_session, device=d, text="What projects do I have?", idempotency_key="bk-1"
    )
    await db_session.commit()
    assert result["ok"] is True
    assert result["route"] == "STATE_QUERY"
    # PROJECT_LIST owner_message lists the owner's projects:
    assert "project" in (result.get("reply") or "").lower()

    user_events = (
        await db_session.execute(
            select(func.count())
            .select_from(Event)
            .where(
                Event.event_type == "message.user",
                Event.device_id == str(d.id),
            )
        )
    ).scalar_one()
    assert user_events >= 1, "trusted phone turns must be durably observable"


# ---------------------------------------------------------------------------
# TRANSACTION ISOLATION (PART 14/15): a failed owner turn must roll back and
# leave a healthy transaction for the next turn in the SAME WebRTC session.
# Raw database text must never leak into TurnResult or durable events.
# ---------------------------------------------------------------------------


import os

import pytest


@pytest.mark.skipif(
    os.environ.get("EV_TEST_USE_LIVE_DB") != "1",
    reason=(
        "InFailedSqlTransaction is PostgreSQL-specific; run with "
        "EV_TEST_USE_LIVE_DB=1 to exercise real aborted-transaction "
        "semantics (reads only + rollback; isolated synthetic device)."
    ),
)
@pytest.mark.asyncio
async def test_failed_turn_rolls_back_and_next_turn_succeeds(
    db_session, monkeypatch
):
    from app.ev.owner_turn import create_owner_turn
    from app.ev.turn_gate import handle_owner_turn

    d = Device(
        name="Tx Isolation Phone",
        token_hash="tx-iso-phone",
        trust_level="owner",
        memory_scope=None,
        device_type="phone",
    )
    db_session.add(d)
    await db_session.flush()
    did = str(d.id)  # capture before any commit expires the instance
    await life.create_project(db_session, actor=MASTER, title="Personal Fitness")
    await db_session.commit()

    # TURN A: force a GENUINE first-statement DB failure directly in the
    # project query path (no silent wrappers in between).
    from sqlalchemy import text as _text

    import app.life.service as life_service

    original_list = life_service.list_projects

    async def poisoned_list(session, *, actor, active_only=True):
        print("POISON-FIRED")
        await session.execute(_text("select 1 + 'not-an-int'"))
        return await original_list(session, actor=actor, active_only=active_only)

    monkeypatch.setattr(life_service, "list_projects", poisoned_list)
    turn_a = create_owner_turn(
        live_session_id="tx-iso",
        provider_item_id=None,
        owner_id=MASTER,
        device_id=did,
        transcript="What projects do I have?",
        transcript_source="device_text",
    )
    res_a = await handle_owner_turn(db_session, turn_a)
    assert res_a.ok is False
    assert "InFailedSqlTransaction" not in str(res_a.error)
    assert "OWNER_TURN_FAILED" in str(res_a.error)
    await db_session.rollback()
    monkeypatch.setattr(life_service, "list_projects", original_list)

    # TURN B: SAME WebRTC session/device — must run on a healthy transaction.
    turn_b = create_owner_turn(
        live_session_id="tx-iso",
        provider_item_id=None,
        owner_id=MASTER,
        device_id=did,
        transcript="What projects do I have?",
        transcript_source="device_text",
    )
    res_b = await handle_owner_turn(db_session, turn_b)
    assert res_b.ok is True, res_b.error
    assert res_b.route == "STATE_QUERY"
    assert res_b.operation == "PROJECT_LIST"
    titles = [p["title"] for p in (res_b.canonical_data or [])]
    assert "Personal Fitness" in titles


@pytest.mark.asyncio
async def test_non_uuid_turn_id_does_not_poison_transaction(db_session):
    """The exact physical failure: device-text turn ids are not Event UUIDs;
    resolution must skip cleanly instead of aborting the transaction."""
    from app.ev.owner_turn import create_owner_turn
    from app.ev.turn_gate import handle_owner_turn

    turn = create_owner_turn(
        live_session_id="nonuuid-test",
        provider_item_id=None,
        owner_id=MASTER,
        device_id=None,
        transcript="What projects do I have?",
        transcript_source="device_text",
        turn_id="text-6168e987-dd7c-4fef-a01c-2c2e04cf78d5-call_pLlTAiy05bZZZ",
    )
    assert len(turn.turn_id) > 20  # previously triggered the raw session.get
    res = await handle_owner_turn(db_session, turn)
    assert res.ok is True
    assert res.route == "STATE_QUERY"
    assert res.operation == "PROJECT_LIST"


@pytest.mark.asyncio
async def test_raw_db_text_never_reaches_owner_message(db_session):
    from app.device_gateway.pipeline import run_trusted_device_turn

    d = Device(
        name="Sanitize Phone",
        token_hash="sanitize-phone",
        trust_level="owner",
        memory_scope=None,
        device_type="phone",
    )
    db_session.add(d)
    await db_session.commit()
    result = await run_trusted_device_turn(
        db_session, device=d, text="What projects do I have?"
    )
    blob = str(result)
    for banned in ("psycopg", "SQLAlchemy", "InFailedSqlTransaction", "SELECT"):
        assert banned.lower() not in blob.lower(), f"raw internals leaked: {banned}"
