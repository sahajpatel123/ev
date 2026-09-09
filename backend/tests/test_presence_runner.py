"""Presence runner hermetic tests (Goal Presence OS V1).

Offline by construction: no model, no network, no broker peers. Every
external effect degrades to an honest FAILED/WAITING — never faked.
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.everywhere.inbox import list_inbox
from app.presence import runner
from app.presence import service as presence


def _node(
    node_id: str,
    kind: str,
    *,
    depends_on: list[str] | None = None,
    payload: dict[str, Any] | None = None,
    risk: str = "R1",
    effect: str = "",
) -> dict[str, Any]:
    node: dict[str, Any] = {
        "node_id": node_id,
        "kind": kind,
        "target": "CORE",
        "status": "PENDING",
        "expected_effect": effect,
        "risk": risk,
        "depends_on": depends_on or [],
        "verification": "",
        "attempts": 0,
    }
    if payload is not None:
        node["payload"] = payload
    return node


async def _device(
    db_session: AsyncSession, *, name: str = "origin", role: str = "companion"
) -> Any:
    from app.models import Device

    dev = Device(name=name, role=role)
    db_session.add(dev)
    await db_session.flush()
    return dev


def _attempts(row: Any) -> dict[str, int]:
    return {
        str(n.get("node_id")): int(n.get("attempts") or 0)
        for n in (dict(row.graph or {}).get("nodes") or [])
    }


async def test_runner_lifecycle_completes_with_evidence(db_session: AsyncSession) -> None:
    # role=home_station: deliver's companion fan-out would inbox a companion
    # origin twice; a station origin gets exactly the single direct item.
    dev = await _device(db_session, role="home_station")
    row = await presence.create_contract(
        db_session, objective="Assemble the daily note", origin_device_id=dev.id
    )
    body = "hello presence"
    digest = hashlib.sha256(body.encode()).hexdigest()
    row.graph = {
        "nodes": [
            _node("read", "CORE_READ", payload={"read": "situation"}),
            _node(
                "write",
                "ARTIFACT_OPERATION",
                depends_on=["read"],
                payload={"filename": "note.txt", "content": body},
            ),
            _node(
                "check",
                "VERIFY",
                depends_on=["write"],
                payload={"predicate": "artifact_exists", "filename": "note.txt", "sha256": digest},
            ),
            _node(
                "tell",
                "NOTIFY",
                depends_on=["check"],
                payload={"title": "Done", "body": "daily note ready"},
            ),
        ]
    }
    await db_session.flush()

    out = await runner.advance(db_session, row.id)

    assert out["advanced"] is True
    assert out["state"] == "COMPLETED"
    assert out["completion"] == "COMPLETED_VERIFIED"
    assert row.confidence == "COMPLETED_VERIFIED"
    evidence = dict(row.evidence or {})
    assert evidence["artifact:write"]["sha256"] == digest
    assert "core_read:read" in evidence
    assert evidence["verify:check"]["sha256"] == digest
    items = await list_inbox(db_session, device_id=dev.id)
    assert any(item["title"] == "Done" for item in items)
    root = os.environ.get("EV_STORAGE_ROOT", "")
    path = os.path.join(root, "sandbox", "presence", str(row.id), "note.txt")
    with open(path, "rb") as fh:
        assert fh.read() == body.encode()


async def test_parked_dispatches_nothing(db_session: AsyncSession) -> None:
    dev = await _device(db_session)
    row = await presence.create_contract(
        db_session, objective="Parked work", origin_device_id=dev.id
    )
    row.graph = {
        "nodes": [
            _node(
                "write",
                "ARTIFACT_OPERATION",
                payload={"filename": "parked.txt", "content": "must not exist"},
            ),
            _node(
                "tell",
                "NOTIFY",
                depends_on=["write"],
                payload={"title": "Nope", "body": "must not send"},
            ),
        ]
    }
    await presence.transition(db_session, row, "PARKED", reason="owner park")
    await db_session.flush()

    out = await runner.advance(db_session, row.id)

    assert out["advanced"] is False
    assert out["state"] == "PARKED"
    assert out["node_results"] == {}
    statuses = [n.get("status") for n in (row.graph or {}).get("nodes") or []]
    assert statuses == ["PENDING", "PENDING"]
    assert await list_inbox(db_session, device_id=dev.id) == []
    root = os.environ.get("EV_STORAGE_ROOT", "")
    assert not os.path.exists(os.path.join(root, "sandbox", "presence", str(row.id)))


async def test_cancel_propagates_and_blocks_resume(db_session: AsyncSession) -> None:
    from app.ev.research import ResearchService
    from app.models import DeviceRoutedAction
    from app.schemas import ResearchJobCreate

    dev = await _device(db_session)
    row = await presence.create_contract(
        db_session, objective="Cancellable work", origin_device_id=dev.id
    )
    row.graph = {"nodes": [_node("a", "CORE_READ", payload={"read": "mission"})]}
    cond = await presence.add_condition(
        db_session,
        row,
        cond_class="TIME",
        payload={"at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
    )
    db_session.add(
        DeviceRoutedAction(
            action_id=f"presence-{row.id}-n1",
            owner_scope="master",
            requesting_device_id=dev.id,
            capability="device.ping",
            arguments={},
            status="ROUTED",
            risk_class="R1",
            idempotency_key=f"presence-{row.id}-n1",
        )
    )
    job = await ResearchService(db_session, "master").create_job(
        ResearchJobCreate(goal="cancel me please")
    )
    row.evidence = {"research:n1": {"job_id": str(job.id)}}
    await db_session.flush()

    summary = await runner.cancel_contract(db_session, row, "owner stop")

    assert row.state == "CANCELLED"
    assert summary["state"] == "CANCELLED"
    assert summary["nodes"] == 1
    assert summary["conditions"] == 1
    assert summary["actions"] == 1
    assert summary["jobs"] == 1
    assert cond.state == "CANCELLED"
    assert (dict(row.graph or {})["nodes"][0]["status"]) == "CANCELLED"
    detail = await ResearchService(db_session, "master").detail(job.id)
    assert detail is not None and str(detail.status) == "cancelled"

    out = await runner.advance(db_session, row.id)
    assert out["advanced"] is False
    assert out["state"] == "CANCELLED"
    with pytest.raises(ValueError):
        await presence.transition(db_session, row, "ACTIVE", reason="owner resume")


async def test_expired_condition_contract_stalls(db_session: AsyncSession) -> None:
    row = await presence.create_contract(db_session, objective="Expiring wait")
    cond = await presence.add_condition(
        db_session,
        row,
        cond_class="TIME",
        payload={"at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
    )
    cond.state = "EXPIRED"
    await presence.set_wait(
        db_session,
        row,
        wait_state="WAITING_FOR_CONDITION",
        condition={"expect": "a time"},
        reason="waiting",
    )
    await db_session.flush()

    stalled = await runner.detect_stalls(db_session, older_than_s=0)

    assert str(row.id) in stalled
    assert row.state == "STALLED"


async def test_double_advance_no_duplicates(db_session: AsyncSession) -> None:
    dev = await _device(db_session, role="home_station")
    row = await presence.create_contract(
        db_session, objective="Idempotent work", origin_device_id=dev.id
    )
    body = "stable bytes"
    row.graph = {
        "nodes": [
            _node(
                "write", "ARTIFACT_OPERATION", payload={"filename": "stable.txt", "content": body}
            ),
            _node("tell", "NOTIFY", depends_on=["write"], payload={"title": "Hi", "body": "once"}),
        ]
    }
    await db_session.flush()

    first = await runner.advance(db_session, row.id)
    assert first["state"] == "COMPLETED"
    attempts_before = _attempts(row)
    evidence_before = dict(row.evidence or {})
    root = os.environ.get("EV_STORAGE_ROOT", "")
    path = os.path.join(root, "sandbox", "presence", str(row.id), "stable.txt")
    with open(path, "rb") as fh:
        sha_before = hashlib.sha256(fh.read()).hexdigest()

    second = await runner.advance(db_session, row.id)

    assert second["advanced"] is False
    assert second["node_results"] == {}
    assert _attempts(row) == attempts_before
    assert dict(row.evidence or {}) == evidence_before
    with open(path, "rb") as fh:
        assert hashlib.sha256(fh.read()).hexdigest() == sha_before
    items = await list_inbox(db_session, device_id=dev.id)
    assert sum(1 for item in items if item["title"] == "Hi") == 1


async def test_waiting_for_device_path(db_session: AsyncSession) -> None:
    offline = await presence.create_contract(db_session, objective="Offline probe")
    cond = await presence.add_condition(
        db_session, offline, cond_class="DEVICE_ONLINE", payload={"role": "home_station"}
    )
    # No devices exist: home_station is offline, so ONLINE is False.
    assert await presence.evaluate_condition(db_session, cond) is False

    row = await presence.create_contract(db_session, objective="Device-bound work")
    row.graph = {"nodes": [_node("wait", "WAIT_DEVICE", payload={"role": "home_station"})]}
    await db_session.flush()

    out = await runner.advance(db_session, row.id)

    assert out["advanced"] is True
    assert out["state"] == "WAITING_FOR_DEVICE"
    assert (dict(row.graph or {})["nodes"][0]["status"]) == "WAITING"


async def test_research_failure_stays_active(db_session: AsyncSession) -> None:
    row = await presence.create_contract(db_session, objective="Offline research")
    row.graph = {"nodes": [_node("r", "RESEARCH")]}
    await db_session.flush()

    out = await runner.advance(db_session, row.id)

    assert out["advanced"] is True
    assert out["node_results"]["r"]["status"] == "FAILED"
    assert row.state == "ACTIVE"


async def test_consider_event_ignores_goal_events(db_session: AsyncSession) -> None:
    row = await presence.create_contract(db_session, objective="Guarded")
    out = await runner.consider_event(db_session, "goal.completed", {"goal_id": str(row.id)})
    assert out.get("ignored") is True
    assert out["evaluated"] == 0


async def test_presence_tick_bounded_counts(db_session: AsyncSession) -> None:
    row = await presence.create_contract(db_session, objective="Tick me")
    row.graph = {"nodes": [_node("tell", "NOTIFY", payload={"title": "T", "body": "B"})]}
    await db_session.flush()

    counts = await runner.presence_tick(db_session, datetime.now(UTC), limit=25)

    assert counts["advanced"] == 1
    assert counts["evaluated"] == 0
    assert row.state == "COMPLETED"
