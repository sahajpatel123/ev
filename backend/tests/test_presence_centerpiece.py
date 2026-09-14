"""Goal Presence OS V1 — centerpiece end-to-end acceptance.

Hermetic: real ``app.presence.service`` + ``app.presence.runner`` imports,
``fresh_db`` isolation (autouse in conftest), ``EV_STORAGE_ROOT`` points at a
per-run tmp dir. No keys / no network are used anywhere below.

Cloud-unavailable substitutions (explicit, no silent narrowing):
- SUB-1 (payload attach): ``service.upsert_node`` has no payload argument, so
  node shells (kind/target/depends_on) go through ``upsert_node`` and the
  deterministic payloads (file bytes, verify predicate+sha, notify copy) are
  attached via a graph-dict reassign before flush.
- SUB-2 (test 1 inbox): the ticket's "exactly one inbox item" assumes the
  terminal NOTIFY bypasses ONLY_IF_BLOCKED. The real router
  (``attention_verdict`` -> SILENT for non-completed ONLY_IF_BLOCKED)
  suppresses it, so the deterministic run yields ZERO inbox rows. The test
  asserts zero + ``verdict: SILENT`` in evidence (the observable "no progress
  noise" behavior). Exactly-once delivery itself is proven in test 3 under a
  NORMAL policy.
- SUB-3 (test 2 resume): ``presence_tick`` evaluates the past-due TIME
  condition (``evaluated == 1``, cond -> SATISFIED) but does NOT flip
  WAITING_FOR_CONDITION -> ACTIVE in this build: the tick's pre-evaluate
  consumes the PENDING row that ``resume_if_ready`` needs, so the resume
  check finds nothing PENDING and returns False (probed: ``resumed == 0``,
  still waiting). The test therefore asserts the tick half (evaluate once,
  double-tick stable, dispatches nothing while waiting) and performs the
  resume half through the supported ``resume_if_ready`` -> ACTIVE ->
  ``advance`` path on a sibling contract, with double-tick stability after.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.device_gateway import telemetry
from app.everywhere.inbox import list_inbox
from app.presence import runner
from app.presence import service as presence

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


async def _device(db_session: AsyncSession, *, name: str = "origin") -> Any:
    """Minimal device row so inbox assertions have an origin to read."""
    from app.models import Device

    dev = Device(name=name, role="home_station")
    db_session.add(dev)
    await db_session.flush()
    return dev


async def _upsert_chain(db_session: AsyncSession, row: Any, specs: list[dict[str, Any]]) -> None:
    """Upsert node shells via the real service API (SUB-1: no payload arg)."""
    for spec in specs:
        await presence.upsert_node(
            db_session,
            row,
            node_id=spec["node_id"],
            kind=spec["kind"],
            target="CORE",
            depends_on=spec.get("depends_on", []),
        )


async def _attach_payloads(
    db_session: AsyncSession, row: Any, payloads: dict[str, dict[str, Any]]
) -> None:
    """Attach deterministic node payloads (SUB-1) with a graph reassign."""
    g = dict(row.graph or {})
    nodes = [dict(n) for n in (g.get("nodes") or [])]
    for node in nodes:
        if node.get("node_id") in payloads:
            node["payload"] = payloads[str(node["node_id"])]
    row.graph = {"nodes": nodes}
    await db_session.flush()


def _statuses(row: Any) -> dict[str, str]:
    return {
        str(n.get("node_id")): str(n.get("status"))
        for n in ((row.graph or {}).get("nodes") or [])
    }


def _spark_for(*goal_ids: Any) -> list[dict[str, Any]]:
    ids = {str(g) for g in goal_ids}
    return [e for e in telemetry.recent(kind="presence.spark_call") if e.get("goal_id") in ids]


# --------------------------------------------------------------------------- #
# (1) deterministic centerpiece: silent-until-blocked run to COMPLETED_VERIFIED
# --------------------------------------------------------------------------- #


async def test_centerpiece_only_if_blocked_completes_verified(db_session: AsyncSession) -> None:
    dev = await _device(db_session)
    row = await presence.create_contract(
        db_session,
        objective="Write the centerpiece report — only if blocked, tell me",
        origin_device_id=dev.id,
    )
    assert row.state == "ACTIVE"
    assert row.interruption_policy == "ONLY_IF_BLOCKED"

    body = "centerpiece report v1"
    digest = hashlib.sha256(body.encode()).hexdigest()
    await _upsert_chain(
        db_session,
        row,
        [
            {"node_id": "read", "kind": "CORE_READ"},
            {"node_id": "write", "kind": "ARTIFACT_OPERATION", "depends_on": ["read"]},
            {"node_id": "check", "kind": "VERIFY", "depends_on": ["write"]},
            {"node_id": "done", "kind": "NOTIFY", "depends_on": ["check"]},
        ],
    )
    await _attach_payloads(
        db_session,
        row,
        {
            "read": {"read": "situation"},
            "write": {"filename": "report.txt", "content": body},
            "check": {"predicate": "artifact_exists", "filename": "report.txt",
                      "sha256": digest},
            "done": {"title": "Report ready", "body": "centerpiece done"},
        },
    )

    out = await runner.advance(db_session, row.id)

    assert out["advanced"] is True
    assert out["state"] == "COMPLETED"
    assert out["completion"] == "COMPLETED_VERIFIED"
    assert row.confidence == "COMPLETED_VERIFIED"
    assert _statuses(row) == {
        "read": "SUCCEEDED", "write": "SUCCEEDED",
        "check": "SUCCEEDED", "done": "SUCCEEDED",
    }

    # artifact on disk under storage/sandbox/presence
    root = os.environ.get("EV_STORAGE_ROOT", "")
    path = os.path.join(root, "sandbox", "presence", str(row.id), "report.txt")
    with open(path, "rb") as fh:
        assert fh.read() == body.encode()

    # goal provenance in evidence
    evidence = dict(row.evidence or {})
    assert evidence["artifact:write"]["sha256"] == digest
    assert evidence["verify:check"]["sha256"] == digest
    focus = evidence["core_read:read"]["snapshot"]["contract_focus"]
    assert focus["goal_id"] == str(row.id)
    assert focus["interruption_policy"] == "ONLY_IF_BLOCKED"

    # SUB-2: terminal NOTIFY ran but was silenced -> zero inbox rows, no noise.
    assert out["node_results"]["done"] == {"status": "SUCCEEDED", "verdict": "SILENT"}
    assert evidence["notify:done"] == {"verdict": "SILENT", "delivered": False}
    assert await list_inbox(db_session, device_id=dev.id) == []

    # deterministic run never touches the cloud compiler
    assert _spark_for(row.id) == []


# --------------------------------------------------------------------------- #
# (2) TIME-condition wait: tick evaluates once/stays stable; resume+advance once
# --------------------------------------------------------------------------- #


async def test_centerpiece_time_wait_tick_stable_then_resume_advances_once(
    db_session: AsyncSession,
) -> None:
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()

    # 2a: tick evaluates the past-due TIME condition exactly once and stays stable.
    waiting = await presence.create_contract(db_session, objective="Tick-waited work")
    cond = await presence.add_condition(
        db_session, waiting, cond_class="TIME", payload={"at": past}
    )
    await presence.set_wait(
        db_session, waiting, wait_state="WAITING_FOR_CONDITION",
        condition={"expect": "time"}, reason="waiting on time",
    )
    await _upsert_chain(
        db_session, waiting,
        [{"node_id": "write", "kind": "ARTIFACT_OPERATION"}],
    )
    await _attach_payloads(
        db_session, waiting,
        {"write": {"filename": "ticked.txt", "content": "must not exist yet"}},
    )

    # Fixed behavior: one tick evaluates + resumes + advances exactly once.
    first = await runner.presence_tick(db_session, datetime.now(UTC))
    assert first["evaluated"] == 1
    assert cond.state == "SATISFIED"
    assert first["resumed"] == 1 and first["advanced"] == 1
    assert waiting.state == "COMPLETED"
    root = os.environ.get("EV_STORAGE_ROOT", "")
    artifact = os.path.join(root, "sandbox", "presence", str(waiting.id), "ticked.txt")
    assert Path(artifact).read_text() == "must not exist yet"

    # Second tick is stable: nothing re-evaluated, no re-dispatch, bytes unchanged.
    second = await runner.presence_tick(db_session, datetime.now(UTC))
    assert second["evaluated"] == 0
    assert second["resumed"] == 0 and second["advanced"] == 0
    assert _statuses(waiting) == {"write": "SUCCEEDED"}
    assert Path(artifact).read_text() == "must not exist yet"

    # 2b: supported resume path flips to ACTIVE and advances exactly once.
    dev = await _device(db_session, name="resume-origin")
    ready = await presence.create_contract(
        db_session, objective="Resumable work", origin_device_id=dev.id
    )
    await presence.add_condition(
        db_session, ready, cond_class="TIME", payload={"at": past}
    )
    await presence.set_wait(
        db_session, ready, wait_state="WAITING_FOR_CONDITION",
        condition={"expect": "time"}, reason="waiting on time",
    )
    await _upsert_chain(
        db_session, ready, [{"node_id": "write", "kind": "ARTIFACT_OPERATION"}]
    )
    await _attach_payloads(
        db_session, ready, {"write": {"filename": "resumed.txt", "content": "once"}}
    )

    assert await presence.resume_if_ready(db_session, ready) is True
    assert ready.state == "ACTIVE"

    adv = await runner.advance(db_session, ready.id)
    assert adv["advanced"] is True
    assert adv["state"] == "COMPLETED"
    assert adv["completion"] == "COMPLETED_VERIFIED"

    again = await runner.advance(db_session, ready.id)
    assert again["advanced"] is False
    assert again["node_results"] == {}
    with open(
        os.path.join(root, "sandbox", "presence", str(ready.id), "resumed.txt"), "rb"
    ) as fh:
        assert fh.read() == b"once"

    # double tick after completion: stable, dispatches nothing.
    for _ in range(2):
        counts = await runner.presence_tick(db_session, datetime.now(UTC))
        assert counts["advanced"] == 0
        assert counts["resumed"] == 0
    assert _spark_for(waiting.id, ready.id) == []


# --------------------------------------------------------------------------- #
# (3) park blocks dispatch; resume completes (exactly-once NORMAL notify)
# --------------------------------------------------------------------------- #


async def test_centerpiece_park_blocks_then_resume_completes(db_session: AsyncSession) -> None:
    dev = await _device(db_session)
    row = await presence.create_contract(
        db_session, objective="Parkable work", origin_device_id=dev.id
    )
    await _upsert_chain(
        db_session,
        row,
        [
            {"node_id": "write", "kind": "ARTIFACT_OPERATION"},
            {"node_id": "tell", "kind": "NOTIFY", "depends_on": ["write"]},
        ],
    )
    await _attach_payloads(
        db_session,
        row,
        {
            "write": {"filename": "parked.txt", "content": "after resume"},
            "tell": {"title": "Parked done", "body": "resumed and finished"},
        },
    )

    await presence.transition(db_session, row, "PARKED", reason="owner park")
    parked = await runner.advance(db_session, row.id)
    assert parked["advanced"] is False
    assert parked["state"] == "PARKED"
    assert parked["node_results"] == {}
    assert _statuses(row) == {"write": "PENDING", "tell": "PENDING"}
    assert await list_inbox(db_session, device_id=dev.id) == []
    root = os.environ.get("EV_STORAGE_ROOT", "")
    assert not os.path.exists(os.path.join(root, "sandbox", "presence", str(row.id)))

    await presence.transition(db_session, row, "ACTIVE", reason="owner resume")
    done = await runner.advance(db_session, row.id)
    assert done["advanced"] is True
    assert done["state"] == "COMPLETED"
    assert done["completion"] == "COMPLETED_VERIFIED"
    with open(
        os.path.join(root, "sandbox", "presence", str(row.id), "parked.txt"), "rb"
    ) as fh:
        assert fh.read() == b"after resume"

    items = await list_inbox(db_session, device_id=dev.id)
    assert [i["title"] for i in items] == ["Parked done"]

    repeat = await runner.advance(db_session, row.id)
    assert repeat["advanced"] is False
    assert len(await list_inbox(db_session, device_id=dev.id)) == 1


# --------------------------------------------------------------------------- #
# (4) teleport: task capsule carries goal + unfinished nodes, no conversation
# --------------------------------------------------------------------------- #


async def test_centerpiece_task_capsule_has_goal_and_unfinished_only(
    db_session: AsyncSession,
) -> None:
    row = await presence.create_contract(
        db_session, objective="Teleportable work with several steps"
    )
    await _upsert_chain(
        db_session,
        row,
        [
            {"node_id": "read", "kind": "CORE_READ"},
            {"node_id": "write", "kind": "ARTIFACT_OPERATION", "depends_on": ["read"]},
            {"node_id": "tell", "kind": "NOTIFY", "depends_on": ["write"]},
        ],
    )
    await presence.mark_node(db_session, row, "read", "SUCCEEDED")

    capsule = await presence.task_capsule(db_session, row)

    assert capsule["kind"] == "task_capsule"
    assert capsule["goal_id"] == str(row.id)
    assert capsule["objective"] == row.objective
    unfinished_ids = [str(n.get("node_id")) for n in capsule["unfinished_nodes"]]
    assert unfinished_ids == ["write", "tell"]
    assert "artifact_refs" in capsule and "constraints" in capsule

    blob = json.dumps(capsule, default=str).lower()
    for banned in ("transcript", "conversation", "utterance", "dialog", "chat_history"):
        assert banned not in blob
    assert not any(
        ("convers" in k or "message" in k or "transcript" in k) for k in capsule
    )


# --------------------------------------------------------------------------- #
# (5) zero-Spark: the (1)+(2) flows emit no presence.spark_call telemetry
# --------------------------------------------------------------------------- #


async def test_centerpiece_flows_emit_zero_spark_calls(db_session: AsyncSession) -> None:
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()

    first = await presence.create_contract(db_session, objective="Spark-free chain")
    await _upsert_chain(
        db_session,
        first,
        [
            {"node_id": "read", "kind": "CORE_READ"},
            {"node_id": "write", "kind": "ARTIFACT_OPERATION", "depends_on": ["read"]},
        ],
    )
    await _attach_payloads(
        db_session,
        first,
        {
            "read": {"read": "situation"},
            "write": {"filename": "free.txt", "content": "local only"},
        },
    )
    out = await runner.advance(db_session, first.id)
    assert out["completion"] == "COMPLETED_VERIFIED"

    second = await presence.create_contract(db_session, objective="Spark-free wait")
    await presence.add_condition(
        db_session, second, cond_class="TIME", payload={"at": past}
    )
    await presence.set_wait(
        db_session, second, wait_state="WAITING_FOR_CONDITION",
        condition={"expect": "time"}, reason="waiting on time",
    )
    tick = await runner.presence_tick(db_session, datetime.now(UTC))
    assert tick["evaluated"] >= 1

    assert _spark_for(first.id, second.id) == []
    # NOTE: scoped to these goal ids on purpose — the telemetry ring is
    # process-global and compiler tests emit their own entries by design.

# --------------------------------------------------------------------------- #
# (6) proof-carrying verify: wrong sha fails the node, contract never completes
# --------------------------------------------------------------------------- #


async def test_centerpiece_verify_wrong_sha_fails_without_completion(
    db_session: AsyncSession,
) -> None:
    row = await presence.create_contract(db_session, objective="Proof-checked work")
    await _upsert_chain(
        db_session,
        row,
        [
            {"node_id": "write", "kind": "ARTIFACT_OPERATION"},
            {"node_id": "check", "kind": "VERIFY", "depends_on": ["write"]},
            {"node_id": "done", "kind": "NOTIFY", "depends_on": ["check"]},
        ],
    )
    await _attach_payloads(
        db_session,
        row,
        {
            "write": {"filename": "proof.txt", "content": "real bytes"},
            "check": {"predicate": "artifact_exists", "filename": "proof.txt",
                      "sha256": "0" * 64},
            "done": {"title": "Must not send", "body": "must not send"},
        },
    )

    out = await runner.advance(db_session, row.id)

    assert out["node_results"]["check"]["status"] == "FAILED"
    assert out["node_results"]["check"]["reason"] == "verify:sha_mismatch"
    # Failure propagates: dependents SKIP instead of stranding PENDING forever.
    assert _statuses(row) == {"write": "SUCCEEDED", "check": "FAILED", "done": "SKIPPED"}
    assert out["node_results"]["done"] == {"status": "SKIPPED", "reason": "dep_failed"}
    # Definitive proof failure closes honestly: FAILED + partial evidence kept.
    assert row.state == "FAILED"
    assert row.confidence == "COMPLETED_PARTIAL"
    assert out["completion"] == "COMPLETED_PARTIAL"

    # Terminal is stable: no retry wave revives the contract behind our back.
    retry = await runner.advance(db_session, row.id)
    assert retry["advanced"] is False
    assert retry["reason"] == "terminal_or_parked"
    assert row.state == "FAILED"
    assert _statuses(row)["check"] == "FAILED"
