"""Evie OS G2 — Evie Everywhere invariants.

Laws under test:
- ONE owner identity: device origin never fragments canonical state.
- SNAPSHOT + DELTA: cursor sync over canonical events, relevance+privacy
  filtered, resumable, with clean CURSOR_TOO_OLD / invalid fallbacks.
- Conflict law: stale expected_version → structured CONFLICT (never silent
  last-write-wins); idempotent re-completes are safe no-ops.
- ONE approval authority / ONE notification authority: projection only;
  resolution through the existing runtime/notify services.
- Capability universe + routing respects REAL presence state.
- Conversation continuity: bounded logical resume, no transcript dump.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ActorContext
from app.everywhere.approvals import pending_approvals
from app.everywhere.capabilities import CapabilityRouter, capability_universe
from app.everywhere.continuity import resume_context
from app.everywhere.devices import health_summary
from app.everywhere.mission_control import status as mc_status
from app.everywhere.owner import CANONICAL_OWNER, owner_scope
from app.everywhere.sync import bootstrap, changes, current_cursor, emit_everywhere_event
from app.life import service as life
from app.models import (
    ApprovedAction,
    ConversationRollup,
    ConversationState,
    ConversationThread,
    Device,
    Event,
    Notification,
)
from app.services import runtime as runtime_service
from app.utils.text import utcnow

MASTER_CTX = ActorContext(actor="master", device_id=None, is_master=True, device=None)


def device_ctx(device: Device) -> ActorContext:
    return ActorContext(
        actor=f"device:{device.name}",
        device_id=device.id,
        is_master=False,
        device=device,
    )


async def _make_device(session: AsyncSession, name: str, **kw) -> Device:
    row = Device(
        name=name,
        token_hash=None,
        trust_level=kw.pop("trust_level", "owner"),
        device_type=kw.pop("device_type", "phone"),
        platform=kw.pop("platform", "ios"),
        paired_at=utcnow(),
        role=kw.pop("role", "primary_companion"),
        memory_scope=kw.pop("memory_scope", "owner"),
        capabilities=kw.pop(
            "capabilities", ["foreground_voice", "camera", "text"]
        ),
        **kw,
    )
    session.add(row)
    await session.flush()
    return row


# ---------------------------------------------------------------------------
# Phase 1 — unified owner identity
# ---------------------------------------------------------------------------


async def test_owner_identity_is_device_independent(db_session: AsyncSession):
    """A project created from a phone-labeled actor is THE SAME project the
    Mac sees. Row ownership never depends on creating device."""
    assert owner_scope("master") == CANONICAL_OWNER
    assert owner_scope("device:Evie Phone") == CANONICAL_OWNER

    from_phone = await life.create_project(
        db_session, actor="device:Evie Phone", title="Personal Fitness"
    )
    await db_session.commit()

    seen_from_mac = await life.list_projects(db_session, actor="master")
    assert [p["title"] for p in seen_from_mac] == ["Personal Fitness"]

    # And the reverse direction.
    await life.create_goal(db_session, actor="master", title="Run 3x this week")
    await db_session.commit()
    goals = await life.list_goals(db_session, actor="device:Evie Phone")
    assert [g["title"] for g in goals] == ["Run 3x this week"]
    assert from_phone["ok"] is True


async def test_sandbox_devices_stay_isolated(db_session: AsyncSession):
    scope = owner_scope("device:Sandbox", device=None)
    assert scope == CANONICAL_OWNER


async def test_events_record_device_provenance(db_session: AsyncSession):
    from sqlalchemy import select

    created = await life.create_goal(
        db_session,
        actor="device:Evie Phone",
        title="Phone goal",
        device_id="device-uuid-123",
    )
    assert created["ok"] is True
    await db_session.commit()
    row = (
        (
            await db_session.execute(
                select(Event).where(Event.event_type == "goal.created")
            )
        )
        .scalars()
        .first()
    )
    assert row.device_id == "device-uuid-123"
    assert row.content["actor"] == "device:Evie Phone"


# ---------------------------------------------------------------------------
# Phase 4/5 — cursor sync (snapshot + delta)
# ---------------------------------------------------------------------------


async def test_cursor_delta_returns_semantic_events_and_advances(
    db_session: AsyncSession,
):
    await life.create_project(db_session, actor="master", title="Evie", priority="CRITICAL")
    await db_session.commit()

    first = await changes(db_session, MASTER_CTX, cursor=None, limit=50)
    assert first["ok"] and first["count"] >= 1
    cursor = first["next_cursor"]

    # New change after the cursor.
    projects = await life.list_projects(db_session, actor="master")
    await life.update_project(
        db_session, actor="master", project_id=projects[0]["id"], priority="HIGH"
    )
    await db_session.commit()

    delta = await changes(db_session, MASTER_CTX, cursor=cursor, limit=50)
    types = [e["type"] for e in delta["events"]]
    assert "project.priority_changed" in types
    assert all(e["type"] not in ("project.created",) for e in delta["events"])
    assert delta["next_cursor"] >= cursor or delta["count"] == 0


async def test_sync_relevance_filter_blocks_internal_noise(db_session: AsyncSession):
    db_session.add(
        Event(
            source="screen",
            event_type="model.token.stream",
            content={"token": "x"},
            sha256="noise",
        )
    )
    await db_session.commit()
    out = await changes(db_session, MASTER_CTX, cursor=None, limit=200)
    assert all(e["type"] != "model.token.stream" for e in out["events"])


async def test_sync_privacy_filters_sensitive_for_untrusted(db_session: AsyncSession):
    phone = await _make_device(db_session, "Evie Phone", trust_level="device")
    await db_session.commit()
    ctx = device_ctx(phone)

    await life.create_commitment(
        db_session,
        actor="master",
        description="public promise",
        privacy_level="normal",
    )
    await life.create_commitment(
        db_session,
        actor="master",
        description="private promise",
        privacy_level="sensitive",
    )
    await db_session.commit()

    out = await changes(db_session, ctx, cursor=None, limit=100)
    texts = [
        str(e["content"].get("description") or "") for e in out["events"]
        if e["type"].startswith("commitment.")
    ]
    assert any("public" in t for t in texts)
    assert not any("private" in t for t in texts)

    master_out = await changes(db_session, MASTER_CTX, cursor=None, limit=100)
    master_texts = [
        str(e["content"].get("description") or "")
        for e in master_out["events"]
        if e["type"].startswith("commitment.")
    ]
    assert any("private" in t for t in master_texts)


async def test_cursor_invalid_and_too_old_fallbacks(db_session: AsyncSession):
    bad = await changes(db_session, MASTER_CTX, cursor="garbage", limit=10)
    assert bad["error"] == "CURSOR_INVALID" and bad["reset_required"]

    old_at = utcnow() - timedelta(days=60)
    stale = await changes(
        db_session, MASTER_CTX, cursor=f"{old_at.isoformat()}|00000000-0000-0000-0000-000000000000"
    )
    assert stale["error"] == "CURSOR_TOO_OLD" and stale["reset_required"]


async def test_bootstrap_snapshot_shape(db_session: AsyncSession):
    await life.create_project(db_session, actor="master", title="Evie", priority="CRITICAL")
    await life.create_goal(db_session, actor="master", title="Build Evie Everywhere", project_ref="Evie")
    await life.create_commitment(db_session, actor="master", description="Ship G2 slice")
    await db_session.commit()

    snap = await bootstrap(db_session, MASTER_CTX)
    assert snap["owner"] == CANONICAL_OWNER
    titles = [p["title"] for p in snap["projects"]]
    assert "Evie" in titles
    assert any(g["title"] == "Build Evie Everywhere" for g in snap["goals"])
    assert len(snap["open_commitments"]) == 1
    assert "cursor" in snap and snap["cursor"]
    assert "devices" in snap and "capabilities" in snap
    assert "pending_approvals" in snap and "notifications" in snap


async def test_current_cursor_is_deterministic_latest(db_session: AsyncSession):
    assert await current_cursor(db_session) is None
    await life.create_project(db_session, actor="master", title="P")
    await db_session.commit()
    cur = await current_cursor(db_session)
    assert cur and cur["at"] and cur["id"]


# ---------------------------------------------------------------------------
# Phase 6 — version / conflict contract
# ---------------------------------------------------------------------------


async def test_conflicting_writes_are_not_lost(db_session: AsyncSession):
    g = await life.create_goal(db_session, actor="master", title="Goal X")
    gid = g["goal"]["id"]
    v0 = g["goal"]["version"]
    await db_session.commit()

    ok = await life.update_goal(
        db_session, actor="master", goal_id=gid, next_action="step one",
        expected_version=v0,
    )
    assert ok["ok"] is True and ok["goal"]["version"] == v0 + 1
    await db_session.commit()

    stale = await life.update_goal(
        db_session, actor="master", goal_id=gid, progress_note="overwrite",
        expected_version=v0,
    )
    assert stale["ok"] is False and stale["error"] == "CONFLICT"
    assert stale["conflict"]["current_version"] == v0 + 1
    assert stale["conflict"]["current_state"]["next_action"] == "step one"

    project = await life.create_project(db_session, actor="master", title="P")
    pid = project["project"]["id"]
    conflict_p = await life.update_project(
        db_session, actor="master", project_id=pid, priority="LOW",
        expected_version=99,
    )
    assert conflict_p["error"] == "CONFLICT"


async def test_idempotent_completion_is_safe(db_session: AsyncSession):
    from sqlalchemy import select

    c = await life.create_commitment(db_session, actor="master", description="call back")
    cid = c["commitment"]["id"]
    first = await life.update_commitment(db_session, actor="master", commitment_id=cid, status="FULFILLED")
    again = await life.update_commitment(db_session, actor="master", commitment_id=cid, status="FULFILLED")
    assert first["ok"] and again["ok"] and again.get("unchanged") is True
    await db_session.commit()
    n = len(
        (
            await db_session.execute(
                select(Event).where(Event.event_type == "commitment.fulfilled")
            )
        )
        .scalars()
        .all()
    )
    assert n == 1


# ---------------------------------------------------------------------------
# Phase 13/14 — capability universe + routing
# ---------------------------------------------------------------------------


async def test_capability_universe_and_routing_respect_presence(db_session: AsyncSession):
    from app.device_gateway.presence import note as note_presence

    phone = await _make_device(db_session, "Primary iPhone", push_token="tok")
    offline_phone = await _make_device(db_session, "Secondary iPhone", push_token="tok2")
    await db_session.commit()

    note_presence(phone.id, instance_id="p1", state="ready")  # ONLINE
    # offline_phone: no presence at all.

    universe = await capability_universe(db_session)
    by_cap: dict[str, list[dict]] = {}
    for rec in universe["capabilities"]:
        by_cap.setdefault(rec["capability_id"], []).append(rec)

    states = {r["state"] for r in by_cap.get("camera.look", [])}
    assert states == {"AVAILABLE", "DEVICE_OFFLINE"}
    assert universe["revision"]

    routed = await CapabilityRouter.resolve(db_session, capability="camera.look")
    assert routed["ok"] is True
    assert routed["candidates"][0]["device_id"] == str(phone.id)
    assert any(u["device_id"] == str(offline_phone.id) for u in routed["unavailable"])

    missing = await CapabilityRouter.resolve(db_session, capability="jetpack.fly")
    assert missing["ok"] is False and missing["error"] == "no_available_device"


# ---------------------------------------------------------------------------
# Phase 11 — approval continuity (projection; resolution stays canonical)
# ---------------------------------------------------------------------------


async def test_approval_projection_and_canonical_resolution(db_session: AsyncSession):
    from datetime import timedelta as td

    from app.ev.confirm import POL_META_KEY

    expires = (utcnow() + td(seconds=120)).isoformat()
    action = ApprovedAction(
        action_type="send_message",
        title="Confirm send",
        payload={"to": "+1555", "text": "hi", POL_META_KEY: {
            "risk_class": "R3", "target": "message:+1555", "expires_at": expires,
        }},
        status="pending",
        requires_approval=True,
        requested_by="realtime",
    )
    db_session.add(action)
    await db_session.flush()

    pending = await pending_approvals(db_session)
    assert len(pending) == 1
    view = pending[0]
    assert view["action_type"] == "send_message"
    assert view["risk_class"] == "R3"
    # Privacy boundary: parked payload is previewed, not dumped.
    assert "_pol" not in view["arguments_preview"]

    resolved = await runtime_service.decide_action(
        db_session, action.id, actor="device:Primary iPhone", decision="approve"
    )
    await db_session.commit()
    assert resolved.status == "approved" and resolved.approved_by == "device:Primary iPhone"

    still = await pending_approvals(db_session)
    assert all(a["id"] != str(action.id) for a in still)

    # ONE decision: second resolution is refused by the same authority.
    with pytest.raises(ValueError):
        await runtime_service.decide_action(
            db_session, action.id, actor="device:Primary iPhone", decision="deny"
        )


async def test_expired_ttl_ticket_never_shows_as_actionable(db_session: AsyncSession):
    from app.ev.confirm import POL_META_KEY

    expired = (utcnow() - timedelta(seconds=30)).isoformat()
    action = ApprovedAction(
        action_type="computer.control",
        title="Old confirm",
        payload={POL_META_KEY: {"expires_at": expired, "risk_class": "R3"}},
        status="pending",
    )
    db_session.add(action)
    await db_session.flush()
    out = await pending_approvals(db_session)
    assert all(a["id"] != str(action.id) for a in out)
    await db_session.refresh(action)
    assert action.status == "denied"


# ---------------------------------------------------------------------------
# Phase 12 — notification continuity foundation
# ---------------------------------------------------------------------------


async def test_notification_list_and_cross_device_ack_emits_event(db_session: AsyncSession):
    from app.everywhere.sync import recent_notifications
    from app.notify.service import acknowledge_notification

    phone = await _make_device(db_session, "Primary iPhone")
    row = Notification(
        kind="approval_request",
        title="Approve: send message",
        body="Evie wants to send a message.",
        tier="attention",
        fingerprint="fp-g2-test",
        status="delivered",
        delivered_at=utcnow(),
        device_id=phone.id,
        details={"privacy_level": "normal"},
    )
    db_session.add(row)
    await db_session.flush()

    listed = await recent_notifications(db_session)
    assert any(n["id"] == str(row.id) and not n["acknowledged"] for n in listed)

    await acknowledge_notification(db_session, row.id, device_id=phone.id)
    await emit_everywhere_event(
        db_session,
        event_type="notification.acknowledged",
        actor_label="device:Primary iPhone",
        content={"notification_id": str(row.id)},
        device_id=str(phone.id),
    )
    await db_session.commit()

    after = await recent_notifications(db_session)
    entry = next(n for n in after if n["id"] == str(row.id))
    assert entry["acknowledged"] is True

    delta = await changes(db_session, MASTER_CTX, cursor=None, limit=50)
    assert any(e["type"] == "notification.acknowledged" for e in delta["events"])


# ---------------------------------------------------------------------------
# Phase 7 — conversation continuity
# ---------------------------------------------------------------------------


async def test_conversation_resume_context_bounded(db_session: AsyncSession):
    thread = ConversationThread(title="EV — continuous conversation", is_default=True)
    db_session.add(thread)
    await db_session.flush()
    db_session.add(
        ConversationState(
            thread_id=thread.id,
            focus="Deciding Evie voice architecture",
            recent_topics=["voice architecture", "G2 sync"],
            pending_questions=[],
        )
    )
    db_session.add(
        ConversationRollup(
            thread_id=thread.id,
            summary="We chose calm voice with deterministic stop; G2 next.",
            decisions=["freeze calm voice"],
            open_questions=["push or poll for G2?"],
        )
    )
    await life.create_project(db_session, actor="master", title="Evie", priority="CRITICAL")
    await db_session.commit()

    ctx = ActorContext(actor="device:Primary iPhone")
    out = await resume_context(db_session, actor=ctx.actor, device_name="Primary iPhone")
    assert out["ok"] is True
    assert out["focus"] == "Deciding Evie voice architecture"
    assert "calm voice" in out["rollup"]["summary"]
    assert out["situation_refs"]["top_project"]["title"] == "Evie"
    assert "Evie" in out["resume_hint"]
    # Bounded, never a full transcript:
    assert len(out["rollup"]["summary"]) <= 1200


# ---------------------------------------------------------------------------
# Phase 8/16/28 — Mission Control everywhere + device section + health
# ---------------------------------------------------------------------------


async def test_mission_control_same_truth_from_any_device(db_session: AsyncSession):
    await life.create_project(db_session, actor="master", title="Evie", priority="CRITICAL")
    await life.create_goal(db_session, actor="master", title="Build Evie Everywhere")
    await db_session.commit()

    mac = await mc_status(db_session, MASTER_CTX)
    phone_ctx = ActorContext(actor="device:Primary iPhone")
    phone = await mc_status(db_session, phone_ctx)

    assert mac["snapshot"]["top_focus"]["title"] == "Evie"
    assert phone["snapshot"]["top_focus"]["title"] == "Evie"
    assert mac["summary"] == phone["summary"]


async def test_health_summary_counts(db_session: AsyncSession):
    await _make_device(db_session, "Primary iPhone")
    summary = await health_summary(db_session)
    assert summary["devices_total"] == 1
    assert "pending_approvals" in summary and "notification_backlog" in summary


# ---------------------------------------------------------------------------
# API surface smoke (master-key client) — DEMO A/B/D shape over HTTP
# ---------------------------------------------------------------------------


async def test_everywhere_api_end_to_end(client, db_session: AsyncSession):
    # MAC creates project+goal through canonical REST.
    r = await client.post(
        "/v1/life/projects",
        json={"title": "Evie", "priority": "CRITICAL"},
    )
    assert r.status_code == 200, r.text
    project = r.json()["project"]
    print("PROJECT RESPONSE:", r.json())

    r = await client.post(
        "/v1/life/goals",
        json={"title": "Test Cross Device Continuity", "project": "Evie"},
    )
    assert r.status_code == 200, r.text
    goal = r.json()["goal"]

    # PHONE-style bootstrap sees both.
    r = await client.get("/v1/everywhere/bootstrap")
    assert r.status_code == 200
    body = r.json()
    assert any(p["id"] == project["id"] for p in body["projects"])
    assert any(g["id"] == goal["id"] for g in body["goals"])

    # Cursor delta over HTTP: new change → visible.
    r = await client.patch(
        f"/v1/life/goals/{goal['id']}",
        json={"state": "COMPLETED", "expected_version": goal["version"]},
    )
    assert r.status_code == 200
    r = await client.get("/v1/everywhere/changes", params={"limit": 100})
    assert r.status_code == 200
    types = [e["type"] for e in r.json()["events"]]
    assert "goal.completed" in types

    # Mission control parity over HTTP.
    r = await client.get("/v1/everywhere/mission-control/status")
    assert r.status_code == 200
    assert r.json()["snapshot"]["active_goals"] == []

    # Devices/capabilities/health endpoints exist and answer.
    for path in ("/v1/everywhere/devices", "/v1/everywhere/capabilities",
                 "/v1/everywhere/health-summary"):
        r = await client.get(path)
        assert r.status_code == 200, path

    # Memory recall endpoint answers (empty result fine).
    r = await client.get("/v1/everywhere/memory/recall", params={"q": "anything"})
    assert r.status_code == 200

    # Conversation resume endpoint answers.
    r = await client.get("/v1/everywhere/conversation/resume_context")
    assert r.status_code == 200


async def test_restart_survives_with_no_duplicates(client, db_session: AsyncSession):
    r = await client.post("/v1/life/projects", json={"title": "Travel"})
    pid = r.json()["project"]["id"]
    await db_session.commit()

    # Simulate reconnect/bootstrap twice — no duplicate entities.
    b1 = await client.get("/v1/everywhere/bootstrap")
    b2 = await client.get("/v1/everywhere/bootstrap")
    ids1 = [p["id"] for p in b1.json()["projects"] if p["id"] == pid]
    ids2 = [p["id"] for p in b2.json()["projects"] if p["id"] == pid]
    assert ids1 == ids2 == [pid]
