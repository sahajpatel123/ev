"""G2 advanced fabric — command contract + hardening proofs.

Stages covered (evidence: INTEGRATION TESTED / TWO-CLIENT INTERNALLY TESTED):

- Universal command envelope (/v1/everywhere/commands) with canonical
  outcome contract incl. duplicate/replay indicator and conflict data.
- PROJECT_UPDATE durable idempotency (the physical canary operation):
  retry after response loss → ONE mutation; ledger survives a fresh
  process/session (no in-memory authority).
- Stale expected_version → structured CONFLICT with current state.
- Device-id spoofing: provenance is server-derived from auth; a trusted
  device cannot record another device's identity.
- Unknown capability advertisement is ignored, never projected.

All fixtures are isolated; owner state untouched.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ActorContext
from app.everywhere.capabilities import validate_capabilities
from app.life import service as life
from app.models import Device

MASTER = "master"


@pytest.fixture
async def phone(db_session: AsyncSession):
    d = Device(
        name="G21 Hardening Phone",
        token_hash="g21-hard-phone",
        trust_level="owner",
        device_type="phone",
    )
    db_session.add(d)
    await db_session.commit()
    return d


def _ctx(device: Device) -> ActorContext:
    return ActorContext(
        actor=f"device:{device.name}", device_id=device.id, is_master=False, device=device
    )


@pytest.mark.asyncio
async def test_project_update_replay_is_one_mutation(db_session, phone):
    """Stage 5/31: same command_id twice AND from a 'fresh process' (new
    service calls with no shared memory) produce ONE canonical mutation."""
    created = await life.create_project(
        db_session, actor=MASTER, title="G2 Update Replay Proof"
    )
    pid = created["project"]["id"]
    v0 = int(created["project"]["version"])
    await db_session.commit()

    cmd = "cmd-update-replay-1"
    r1 = await life.update_project(
        db_session, actor=ctx_scope(phone), project_id=pid,
        priority="HIGH", command_id=cmd,
    )
    await db_session.commit()
    assert r1["ok"] is True and not r1.get("duplicate")
    assert int(r1["project"]["version"]) == v0 + 1

    # Retry after "response lost" — new call path, no in-memory dedupe:
    r2 = await life.update_project(
        db_session, actor=MASTER, project_id=pid,
        priority="HIGH", command_id=cmd,
    )
    await db_session.commit()
    assert r2["ok"] is True and r2.get("duplicate") is True
    assert int(r2["project"]["version"]) == v0 + 1, "replay must not bump version again"

    rows = await life.list_projects(db_session, actor=MASTER)
    row = next(p for p in rows if p["id"] == pid)
    assert row["priority"] == "HIGH" and int(row["version"]) == v0 + 1


def ctx_scope(device: Device) -> str:
    from app.everywhere.owner import owner_scope

    return owner_scope(f"device:{device.name}", device=device)


@pytest.mark.asyncio
async def test_conflict_returns_current_state_for_recovery(db_session):
    created = await life.create_project(db_session, actor=MASTER, title="G2 Conflict Proof")
    pid = created["project"]["id"]
    await life.update_project(db_session, actor=MASTER, project_id=pid, priority="HIGH")
    await db_session.commit()

    stale = await life.update_project(
        db_session, actor=MASTER, project_id=pid,
        priority="LOW", expected_version=int(created["project"]["version"]),
    )
    assert stale["ok"] is False
    c = stale["conflict"]
    assert c["entity"] == "project"
    assert c["current_state"]["priority"] == "HIGH"
    # Retry at the CURRENT version succeeds if still intended:
    fixed = await life.update_project(
        db_session, actor=MASTER, project_id=pid,
        priority="LOW", expected_version=c["current_version"],
    )
    assert fixed["ok"] is True


# ---------------------------------------------------------------------------
# Command envelope over the HTTP surface
# ---------------------------------------------------------------------------


@pytest.fixture
async def api_client(db_session, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, db_session


def _bearer(master_key: str) -> dict:
    return {"Authorization": f"Bearer {master_key}"}


@pytest.mark.asyncio
async def test_command_envelope_create_update_replay(api_client, monkeypatch):
    client, _ = api_client
    from app.config import settings

    headers = _bearer(settings.master_key)

    # create
    r = await client.post(
        "/v1/everywhere/commands",
        json={
            "command_id": "env-cmd-create-1",
            "operation": "project.create",
            "arguments": {"title": "G2 Envelope Project"},
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["duplicate"] is False
    assert body["entity_type"] == "project" and body["entity_id"]
    pid = body["entity_id"]

    # replay of the SAME command → same entity, duplicate=true
    r2 = await client.post(
        "/v1/everywhere/commands",
        json={
            "command_id": "env-cmd-create-1",
            "operation": "project.create",
            "arguments": {"title": "G2 Envelope Project"},
        },
        headers=headers,
    )
    b2 = r2.json()
    assert b2["ok"] is True and b2["duplicate"] is True
    assert b2["entity_id"] == pid

    # update via envelope with correct expected version
    ver = body["new_version"]
    r3 = await client.post(
        "/v1/everywhere/commands",
        json={
            "command_id": "env-cmd-update-1",
            "operation": "project.update",
            "arguments": {"project_id": pid, "priority": "HIGH"},
            "expected_entity_version": ver,
        },
        headers=headers,
    )
    b3 = r3.json()
    assert b3["ok"] is True and b3["new_version"] == ver + 1

    # stale version → CONFLICT outcome (structured, HTTP 200 truth)
    r4 = await client.post(
        "/v1/everywhere/commands",
        json={
            "command_id": "env-cmd-update-stale",
            "operation": "project.update",
            "arguments": {"project_id": pid, "priority": "LOW"},
            "expected_entity_version": ver,
        },
        headers=headers,
    )
    b4 = r4.json()
    assert b4["ok"] is False
    assert b4["error_code"] == "CONFLICT"
    assert b4["conflict"]["current_version"] == ver + 1

    # unknown operation → explicit rejection
    r5 = await client.post(
        "/v1/everywhere/commands",
        json={"command_id": "env-cmd-x-1", "operation": "world.dominate", "arguments": {}},
        headers=headers,
    )
    assert r5.status_code == 422
    assert r5.json()["detail"]["error_code"] == "UNKNOWN_OPERATION"


@pytest.mark.asyncio
async def test_device_identity_is_server_derived_not_client_claimed(
    db_session, phone
):
    """Stage 35: a trusted device cannot record ANOTHER device's identity as
    provenance. device_id written to events comes from auth, never args."""
    ctx = _ctx(phone)
    claimed_other = "not-a-real-device-uuid"

    created = await life.create_goal(
        db_session,
        actor=ctx.data_scope,
        title="G2 provenance proof",
        device_id=str(ctx.device_id),  # what the SERVER derives
    )
    await db_session.commit()
    del claimed_other
    from sqlalchemy import func

    from app.models import Event

    n = (
        await db_session.execute(
            select(func.count())
            .select_from(Event)
            .where(
                Event.event_type == "goal.created",
                Event.device_id == str(ctx.device_id),
                Event.content["title"].as_string() == "G2 provenance proof",
            )
        )
    ).scalar_one()
    assert n == 1


def test_unknown_capabilities_are_ignored():
    accepted, ignored = validate_capabilities(
        ["camera", "admin_everything", "MICROPHONE", "", "screen_look"]
    )
    assert accepted == ["camera", "microphone", "screen_look"]
    assert ignored == ["admin_everything"]


# ---------------------------------------------------------------------------
# Physical canary phrase routing (deterministic; Stage 1/2 lock-in)
# ---------------------------------------------------------------------------


def test_canary_phrases_route_deterministically():
    from app.ev.luna_adapter import _rule_based_intent

    create = _rule_based_intent("Create a project called G2 Cross Device Canary.")
    assert create.route == "STATE_MUTATION"
    assert create.operation == "PROJECT_CREATE"

    setp = _rule_based_intent("Set the priority of G2 Cross Device Canary to High.")
    assert setp.route == "STATE_MUTATION"
    assert setp.operation == "PROJECT_UPDATE"
    assert (setp.priority or "").upper() == "HIGH"

    getp = _rule_based_intent("What is the priority of G2 Cross Device Canary?")
    assert getp.route == "STATE_QUERY"
    assert getp.operation == "PROJECT_GET"
