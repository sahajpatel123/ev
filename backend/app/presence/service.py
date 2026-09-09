"""Presence OS V1 — orchestration service over Core truth.

Contracts layer over goals.* rows; conditions wait event-first; attention
routes through inbox/APNs; capsules move bounded context between devices.
No model calls here: Spark compiles graphs at the call sites that need
reasoning; this module persists semantic state and enforces the fences.

Side-effect law: resume/claim paths are idempotent by stable keys
(contract id / node id / condition id). Retries never duplicate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PresenceCondition, PresenceContract
from app.presence.contract import (
    WAITING_STATES,
    AttentionVerdict,
    ConditionClass,
    ConditionState,
    GoalState,
    InterruptionPolicy,
    NodeStatus,
    ShortCommand,
    can_transition,
    public_contract,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _emit(
    session: AsyncSession,
    event_type: str,
    content: dict[str, Any],
    device_id: Any = None,
) -> None:
    from app.everywhere.sync import emit_everywhere_event

    await emit_everywhere_event(
        session,
        event_type=event_type,
        actor_label="presence",
        content=content,
        device_id=str(device_id) if device_id else None,
    )


# --- Contracts ---


async def create_contract(
    session: AsyncSession,
    *,
    objective: str,
    origin_device_id: Any = None,
    normalized_objective: str = "",
    success_criteria: dict | None = None,
    constraints: dict | None = None,
    deadline_at: datetime | None = None,
    priority: str = "NORMAL",
    interruption_policy: str = "NORMAL",
    autonomy_policy: str = "SAFE_DIGITAL",
    risk_ceiling: str = "R2",
    linked_goal_id: Any = None,
    activate: bool = True,
) -> PresenceContract:
    from app.presence.contract import (
        autonomy_from_text as _auto,
    )
    from app.presence.contract import (
        interruption_from_text as _interrupt,
    )

    row = PresenceContract(
        linked_goal_id=linked_goal_id,
        origin_device_id=origin_device_id,
        objective=(objective or "").strip()[:2000],
        normalized_objective=(normalized_objective or "").strip()[:2000],
        success_criteria=success_criteria or {},
        constraints=constraints or {},
        deadline_at=deadline_at,
        priority=(priority or "NORMAL").upper()[:16],
        interruption_policy=_interrupt(objective).value
        if interruption_policy == "NORMAL"
        else interruption_policy,
        autonomy_policy=_auto(objective).value
        if autonomy_policy == "SAFE_DIGITAL"
        else autonomy_policy,
        risk_ceiling=(risk_ceiling or "R2")[:8],
        state=GoalState.ACTIVE.value if activate else GoalState.DRAFT.value,
    )
    session.add(row)
    await session.flush()
    await _emit(
        session,
        "goal.contract_created",
        {"goal_id": str(row.id), "objective": row.objective[:280]},
        row.origin_device_id,
    )
    if activate:
        await _emit(
            session,
            "goal.activated",
            {"goal_id": str(row.id)},
            row.origin_device_id,
        )
    return row


async def get_contract(
    session: AsyncSession, goal_id: Any
) -> PresenceContract | None:
    try:
        gid = goal_id if isinstance(goal_id, UUID) else UUID(str(goal_id))
    except (ValueError, TypeError, AttributeError):
        return None
    return await session.get(PresenceContract, gid)


async def list_contracts(
    session: AsyncSession,
    *,
    states: list[str] | None = None,
    limit: int = 50,
) -> list[PresenceContract]:
    q = select(PresenceContract).order_by(PresenceContract.updated_at.desc())
    if states:
        q = q.where(PresenceContract.state.in_(states))
    return list((await session.execute(q.limit(max(1, min(limit, 100))))).scalars().all())


async def transition(
    session: AsyncSession,
    row: PresenceContract,
    to: str,
    *,
    reason: str = "",
    confidence: str = "",
    evidence: dict | None = None,
) -> PresenceContract:
    target = GoalState(to)
    if not can_transition(row.state, target.value):
        raise ValueError(f"Illegal contract transition {row.state} -> {target.value}")
    row.state = target.value
    if reason:
        row.blocked_reason = reason[:1000]
    if confidence:
        row.confidence = confidence[:24]
    if evidence:
        merged = dict(row.evidence or {})
        merged.update(evidence)
        row.evidence = merged
    row.version = int(row.version or 0) + 1
    await session.flush()
    event = {
        "goal.completed": "goal.completed",
        "goal.cancelled": "goal.cancelled",
        "goal.parked": "goal.parked",
        "goal.expired": "goal.expired",
        "goal.failed": "goal.failed",
        "goal.stalled": "goal.stalled",
    }.get(f"goal.{target.value.lower()}", "goal.waiting" if target.value.startswith("WAITING") else "goal.resumed")
    await _emit(
        session,
        event,
        {"goal_id": str(row.id), "state": target.value, "reason": (reason or "")[:280]},
        row.origin_device_id,
    )
    return row


async def set_wait(
    session: AsyncSession,
    row: PresenceContract,
    *,
    wait_state: str,
    condition: dict | None = None,
    reason: str = "",
) -> PresenceContract:
    row.next_condition = condition or {}
    return await transition(session, row, wait_state, reason=reason)


# --- Graph nodes (stored inside contract.graph JSON; bounded) ---


def _graph(row: PresenceContract) -> dict[str, Any]:
    g = dict(row.graph or {})
    g.setdefault("nodes", [])
    return g


async def upsert_node(
    session: AsyncSession,
    row: PresenceContract,
    *,
    node_id: str,
    kind: str,
    target: str,
    status: str = NodeStatus.PENDING.value,
    effect: str = "",
    risk: str = "R1",
    depends_on: list[str] | None = None,
    verification: str = "",
) -> dict[str, Any]:
    g = _graph(row)
    nodes: list[dict[str, Any]] = [dict(n) for n in g.get("nodes") or []]
    node = {
        "node_id": node_id[:80],
        "kind": kind,
        "target": target,
        "status": status,
        "expected_effect": effect[:500],
        "risk": risk[:8],
        "depends_on": depends_on or [],
        "verification": verification[:500],
        "attempts": 0,
    }
    for i, existing in enumerate(nodes):
        if existing.get("node_id") == node["node_id"]:
            # Idempotent upsert: never reset a terminal node on retry.
            if existing.get("status") in ("SUCCEEDED", "FAILED", "CANCELLED", "SKIPPED"):
                return existing
            node["attempts"] = int(existing.get("attempts") or 0)
            nodes[i] = node
            break
    else:
        nodes.append(node)
    g["nodes"] = nodes[-64:]
    row.graph = g
    row.version = int(row.version or 0) + 1
    await session.flush()
    return node


async def mark_node(
    session: AsyncSession,
    row: PresenceContract,
    node_id: str,
    status: str,
    *,
    evidence: str = "",
) -> dict[str, Any] | None:
    g = _graph(row)
    nodes = [dict(n) for n in g.get("nodes") or []]
    out = None
    for n in nodes:
        if n.get("node_id") == node_id:
            # Fence: terminal nodes never flip on duplicate delivery.
            if n.get("status") in ("SUCCEEDED", "FAILED", "CANCELLED", "SKIPPED"):
                return n
            n["status"] = status
            n["attempts"] = int(n.get("attempts") or 0) + 1
            if evidence:
                n["evidence"] = evidence[:500]
            out = n
    if out is None:
        return None
    g["nodes"] = nodes
    row.graph = g
    await session.flush()
    return out


# --- Conditions ---


async def add_condition(
    session: AsyncSession,
    row: PresenceContract,
    *,
    cond_class: str,
    payload: dict | None = None,
    source: str = "event",
    strategy: str = "event",
    frequency_s: int = 60,
    ttl_s: int = 86400,
) -> PresenceCondition:
    cond = PresenceCondition(
        contract_id=row.id,
        cond_class=ConditionClass(cond_class).value,
        source=source[:64],
        strategy=strategy[:32],
        frequency_s=max(15, min(frequency_s, 86400)),
        ttl_s=max(60, min(ttl_s, 30 * 86400)),
        state=ConditionState.PENDING.value,
        payload=payload or {},
    )
    session.add(cond)
    await session.flush()
    return cond


def _condition_due(cond: PresenceCondition, now: datetime) -> bool:
    if cond.state != ConditionState.PENDING.value:
        return False
    created = cond.created_at
    if created is not None:
        aware = created if created.tzinfo else created.replace(tzinfo=UTC)
        if (now - aware).total_seconds() > int(cond.ttl_s or 86400):
            return False
    last = cond.last_evaluated_at
    if last is None:
        return True
    aware_last = last if last.tzinfo else last.replace(tzinfo=UTC)
    return (now - aware_last).total_seconds() >= int(cond.frequency_s or 60)


async def evaluate_condition(
    session: AsyncSession,
    cond: PresenceCondition,
    *,
    context: dict | None = None,
) -> bool:
    """Deterministic evaluation. No model calls. True when satisfied."""
    from app.presence.conditions import evaluate as _eval

    now = _utcnow()
    if cond.state != ConditionState.PENDING.value:
        return cond.state == ConditionState.SATISFIED.value
    created = cond.created_at
    if created is not None:
        aware = created if created.tzinfo else created.replace(tzinfo=UTC)
        if (now - aware).total_seconds() > int(cond.ttl_s or 86400):
            cond.state = ConditionState.EXPIRED.value
            await session.flush()
            return False
    satisfied = await _eval(session, cond, context or {})
    cond.last_evaluated_at = now
    if satisfied:
        cond.state = ConditionState.SATISFIED.value
    await session.flush()
    return satisfied


async def resume_if_ready(
    session: AsyncSession,
    row: PresenceContract,
    *,
    context: dict | None = None,
) -> bool:
    """Evaluate pending conditions; resume contract on first satisfaction."""
    from app.presence.contract import TERMINAL_STATES

    if str(row.state) in {s.value for s in TERMINAL_STATES}:
        return False
    if row.state not in (
        GoalState.WAITING.value,
        GoalState.WAITING_FOR_CONDITION.value,
        GoalState.WAITING_FOR_DEVICE.value,
        GoalState.STALLED.value,
    ):
        return False
    conds = list(
        (
            await session.execute(
                select(PresenceCondition).where(
                    PresenceCondition.contract_id == row.id,
                    PresenceCondition.state == ConditionState.PENDING.value,
                )
            )
        )
        .scalars()
        .all()
    )
    for cond in conds:
        if not _condition_due(cond, _utcnow()):
            continue
        if await evaluate_condition(session, cond, context=context):
            await transition(session, row, GoalState.ACTIVE.value, reason="")
            await _emit(
                session,
                "goal.condition_satisfied",
                {"goal_id": str(row.id), "condition_id": str(cond.id)},
                row.origin_device_id,
            )
            return True
    return False


# --- Attention + delivery ---


def attention_verdict(
    *,
    interruption: str,
    priority: str = "NORMAL",
    approval_required: bool = False,
    failed: bool = False,
    completed: bool = False,
) -> AttentionVerdict:
    if approval_required or failed:
        return AttentionVerdict.URGENT_PUSH
    try:
        policy = InterruptionPolicy(interruption)
    except ValueError:
        policy = InterruptionPolicy.NORMAL
    if policy == InterruptionPolicy.SILENT_UNTIL_COMPLETE:
        return AttentionVerdict.INBOX_ONLY if completed else AttentionVerdict.SILENT
    if policy == InterruptionPolicy.ONLY_IF_BLOCKED:
        return AttentionVerdict.INBOX_ONLY if completed else AttentionVerdict.SILENT
    if policy == InterruptionPolicy.URGENT or (priority or "").upper() == "CRITICAL":
        return AttentionVerdict.URGENT_PUSH
    if completed:
        return AttentionVerdict.DIGEST
    return AttentionVerdict.NORMAL_PUSH if policy == InterruptionPolicy.NORMAL else AttentionVerdict.INBOX_ONLY


async def deliver(
    session: AsyncSession,
    row: PresenceContract,
    *,
    title: str,
    body: str,
    verdict: AttentionVerdict | None = None,
    payload: dict | None = None,
) -> dict[str, Any]:
    """Route one contract notice: inbox always (except SILENT), push per verdict."""
    from app.device_gateway.push import notify_trusted_companions
    from app.everywhere.inbox import push_inbox

    v = verdict or attention_verdict(
        interruption=str(row.interruption_policy or "NORMAL"),
        priority=str(row.priority or "NORMAL"),
    )
    if v == AttentionVerdict.SILENT:
        return {"verdict": v.value, "delivered": False}
    target_id = row.origin_device_id
    item = None
    if target_id is not None:
        try:
            item = await push_inbox(
                session,
                device_id=target_id,
                kind="goal",
                title=title[:160],
                body=body[:1900],
                payload={"goal_id": str(row.id), **(payload or {})},
            )
        except Exception:
            item = None
    pushed = 0
    if v in (AttentionVerdict.NORMAL_PUSH, AttentionVerdict.URGENT_PUSH):
        try:
            pushed = await notify_trusted_companions(
                session,
                kind="goal",
                title=title[:160],
                body=body[:1900],
                payload={"goal_id": str(row.id), **(payload or {})},
            )
        except Exception:
            pushed = 0
    return {
        "verdict": v.value,
        "delivered": True,
        "inbox_item_id": str(getattr(item, "id", "") or "") or None,
        "companions_notified": pushed,
    }


# --- Teleport capsules (bounded context, never raw conversation) ---


async def task_capsule(
    session: AsyncSession, row: PresenceContract
) -> dict[str, Any]:
    focused: dict[str, Any] = {}
    try:
        from app.everywhere.handoff_context import get_context, public_context

        ctx = public_context(await get_context(session)) or {}
        focused = {
            "focused_type": ctx.get("focused_type"),
            "focused_title": ctx.get("focused_title"),
            "focused_project_title": ctx.get("focused_project_title"),
        }
    except Exception:
        focused = {}
    g = _graph(row)
    unfinished = [
        n for n in (g.get("nodes") or []) if n.get("status") in ("PENDING", "RUNNING", "WAITING")
    ]
    return {
        "kind": "task_capsule",
        "goal_id": str(row.id),
        "objective": row.objective,
        "state": row.state,
        "focused": focused,
        "unfinished_nodes": unfinished[:16],
        "artifact_refs": dict(row.artifacts or {}),
        "constraints": dict(row.constraints or {}),
        "next_effect": str((row.next_condition or {}).get("expect") or ""),
        "version": int(row.version or 0),
    }


async def continuation_capsule(
    session: AsyncSession, row: PresenceContract
) -> dict[str, Any]:
    return {
        "kind": "continuation_capsule",
        "goal_id": str(row.id),
        "objective": row.objective,
        "state": row.state,
        "confidence": row.confidence,
        "evidence": dict(row.evidence or {}),
        "artifact_refs": dict(row.artifacts or {}),
        "blocked_reason": row.blocked_reason,
        "next_action": str((row.next_condition or {}).get("expect") or ""),
    }


async def situation(session: AsyncSession, *, device_id: Any = None) -> dict[str, Any]:
    out: dict[str, Any] = {"contract_focus": None}
    try:
        from app.life.service import situation_snapshot

        out["core"] = await situation_snapshot(session, actor="master")
    except Exception as exc:
        out["core"] = {"unavailable": str(type(exc).__name__)}
    try:
        from app.device_gateway.lease import current_lease

        lease = await current_lease(session)
        out["lease_holder_device_id"] = str(getattr(lease, "device_id", "") or "") or None
    except Exception:
        out["lease_holder_device_id"] = None
    try:
        from app.everywhere.devices import presence_state
        from app.models import Device

        rows = (await session.execute(select(Device).where(Device.revoked_at.is_(None)))).scalars().all()
        mine = [d for d in rows if str(getattr(d, "memory_scope", "") or "").lower() != "sandbox"]
        by_role: dict[str, str] = {}
        for d in mine:
            try:
                by_role[str(d.role or "companion")] = str(presence_state(d))
            except Exception:
                continue
        out["devices"] = by_role
    except Exception:
        out["devices"] = {}
    try:
        active = await list_contracts(
            session,
            states=[GoalState.ACTIVE.value, *[s.value for s in WAITING_STATES], GoalState.PARKED.value],
        )
        out["contracts"] = [public_contract(c) for c in active]
        act = [c for c in active if c.state == GoalState.ACTIVE.value]
        if act:
            out["contract_focus"] = public_contract(act[0])
    except Exception:
        out["contracts"] = []
    try:
        from app.everywhere.approvals import pending_approvals

        out["pending_approvals"] = await pending_approvals(session, limit=5)
    except Exception:
        out["pending_approvals"] = []
    try:
        from app.everywhere.handoff_context import get_context, public_context

        out["focus_context"] = public_context(await get_context(session)) or {}
    except Exception:
        out["focus_context"] = {}
    return out


# --- Short commands against a situation ---


async def resolve_short_command(
    session: AsyncSession,
    text: str,
    *,
    device_id: Any = None,
) -> dict[str, Any]:
    from app.presence.contract import classify_short_command

    command = classify_short_command(text)
    if command == ShortCommand.UNKNOWN:
        return {"command": command.value, "resolved": False}
    if command == ShortCommand.STOP:
        return {"command": command.value, "resolved": True, "action": "stop_active"}
    if command in (ShortCommand.CONTINUE, ShortCommand.DO_IT, ShortCommand.FINISH):
        active = await list_contracts(session, states=[GoalState.ACTIVE.value], limit=1)
        waiting = (
            []
            if active
            else await list_contracts(
                session,
                states=[GoalState.WAITING.value, GoalState.WAITING_FOR_CONDITION.value,
                        GoalState.WAITING_FOR_DEVICE.value, GoalState.PARKED.value],
                limit=5,
            )
        )
        if active:
            return {"command": command.value, "resolved": True, "action": "continue",
                    "goal_id": str(active[0].id)}
        if len(waiting) == 1:
            return {"command": command.value, "resolved": True, "action": "resume",
                    "goal_id": str(waiting[0].id)}
        return {"command": command.value, "resolved": False,
                "clarify": len(waiting) > 1,
                "candidates": [str(w.id) for w in waiting]}
    if command == ShortCommand.PARK:
        active = await list_contracts(session, states=[GoalState.ACTIVE.value], limit=1)
        if len(active) == 1:
            return {"command": command.value, "resolved": True, "action": "park",
                    "goal_id": str(active[0].id)}
        return {"command": command.value, "resolved": False, "clarify": True}
    if command == ShortCommand.RESUME:
        parked = await list_contracts(session, states=[GoalState.PARKED.value], limit=5)
        if len(parked) == 1:
            return {"command": command.value, "resolved": True, "action": "resume",
                    "goal_id": str(parked[0].id)}
        return {"command": command.value, "resolved": False,
                "clarify": len(parked) != 1,
                "candidates": [str(w.id) for w in parked]}
    if command in (ShortCommand.MOVE_TO_MAC, ShortCommand.BRING_HERE):
        active = await list_contracts(
            session,
            states=[GoalState.ACTIVE.value, *WAITING_STATES],
            limit=1,
        )
        if active:
            return {"command": command.value, "resolved": True, "action": "teleport",
                    "goal_id": str(active[0].id)}
        return {"command": command.value, "resolved": False, "clarify": True}
    return {"command": command.value, "resolved": True, "action": "answer_from_situation"}


# --- Self-diagnosis (boundary classifier, deterministic) ---


def diagnose_failure(*, error_code: str = "", context: dict | None = None) -> dict[str, str]:
    code = (error_code or "").upper()
    ctx = context or {}
    mapping = [
        ("TARGET_DEVICE_OFFLINE", "device_offline", "wait"),
        ("DEVICE_OFFLINE", "device_offline", "wait"),
        ("WEbrtc_UNAVAILABLE", "provider_unavailable", "retry"),
        ("LEGACY_BRAIN_BLOCKED", "provider_unavailable", "fail"),
        ("CAPABILITY_UNAVAILABLE", "capability_missing", "fail"),
        ("POLICY", "policy_blocked", "ask_owner"),
        ("FORBIDDEN", "policy_blocked", "ask_owner"),
        ("STALE", "stale_ref", "reobserve"),
        ("EXPIRED", "stale_ref", "reobserve"),
        ("NOT_FOUND", "stale_ref", "reobserve"),
        ("AMBIGUOUS", "target_ambiguous", "ask_owner"),
        ("PERMISSION", "permission_denied", "ask_owner"),
        ("NETWORK", "network_unavailable", "retry"),
        ("TIMEOUT", "network_unavailable", "retry"),
        ("VERIF", "verification_failed", "retry"),
    ]
    for needle, boundary, recovery in mapping:
        if needle in code:
            return {"boundary": boundary, "recovery": recovery, "detail": code[:120]}
    if ctx.get("offline"):
        return {"boundary": "device_offline", "recovery": "wait", "detail": code[:120]}
    return {"boundary": "unknown", "recovery": "ask_owner", "detail": code[:120]}


# --- Read-only simulator ---


async def simulate(session: AsyncSession, *, scenario: str) -> dict[str, Any]:
    """What breaks if X? Reads capability/device/contract state; mutates nothing."""
    low = (scenario or "").lower()
    sit = await situation(session)
    devices = sit.get("devices") or {}
    contracts = sit.get("contracts") or []
    home = devices.get("home_station", "OFFLINE")
    if "offline" in low and ("mac" in low or "home" in low or "home station" in low):
        affected = [
            c["goal_id"] for c in contracts
            if "HOME" in str(c.get("next_condition") or {}) or True
        ]
        return {
            "scenario": scenario,
            "mutated": False,
            "home_station_now": home,
            "would_wait": affected,
            "continues": ["CORE reads/writes", "CLOUD research", "phone-local capture"],
        }
    return {
        "scenario": scenario,
        "mutated": False,
        "devices": devices,
        "active_contracts": len([c for c in contracts if c.get("state") == "ACTIVE"]),
    }


# --- What changed (semantic delta via existing changes_since) ---


async def what_changed(
    session: AsyncSession, *, since_hours: int = 24, limit: int = 20
) -> dict[str, Any]:
    try:
        from app.life.service import changes_since

        since = _utcnow() - timedelta(hours=max(1, min(since_hours, 24 * 30)))
        changes = await changes_since(session, actor="master", since=since, limit=limit)
    except Exception:
        changes = []
    contracts = await list_contracts(session, limit=limit)
    return {
        "changes": changes,
        "contracts": [public_contract(c) for c in contracts],
    }


# --- Turn executor for resolved short commands ---


async def presence_turn(
    session: AsyncSession,
    *,
    device: Any,
    resolution: dict[str, Any],
    text: str,
) -> dict[str, Any]:
    """Execute a resolved ShortCommand. Deterministic; no model calls."""
    from app.presence.contract import ShortCommand, owner_line_for

    action = str(resolution.get("action") or "")
    goal_id = resolution.get("goal_id")
    base: dict[str, Any] = {
        "ok": True,
        "route": "PRESENCE",
        "operation": f"presence.{action or 'resolve'}",
        "route_target": "CORE",
        "interference": "NON_DISRUPTIVE",
        "turn_id": None,
    }
    if action == "stop_active":
        active = await list_contracts(session, states=[GoalState.ACTIVE.value], limit=1)
        if not active:
            return {**base, "reply": "Nothing active to stop.", "action_result": "COMPLETED"}
        from app.presence.runner import cancel_contract

        summary = await cancel_contract(session, active[0], "owner stop")
        return {**base, "reply": owner_line_for(GoalState.CANCELLED, title=active[0].objective[:80]),
                "action_result": "COMPLETED", "goal_id": str(active[0].id),
                "cancelled": summary}
    if goal_id is None:
        return {**base, "reply": "I want to be precise — which goal do you mean?",
                "action_result": "NEEDS_CONFIRMATION", "needs_clarification": True}
    found = await get_contract(session, goal_id)
    if found is None:
        return {**base, "reply": "I couldn't find that goal anymore.",
                "action_result": "FAILED", "ok": False}
    row = found
    if action == "continue":
        return {**base, "reply": f"Continuing “{row.objective[:160]}” — it's {row.state.lower()}.",
                "action_result": "COMPLETED", "goal_id": str(row.id)}
    if action == "park":
        row = await transition(session, row, GoalState.PARKED.value, reason="owner park")
        return {**base, "reply": owner_line_for(GoalState.PARKED, title=row.objective[:80]),
                "action_result": "COMPLETED", "goal_id": str(row.id)}
    if action == "resume":
        row = await transition(session, row, GoalState.ACTIVE.value, reason="owner resume")
        return {**base, "reply": f"Resumed “{row.objective[:160]}”.",
                "action_result": "COMPLETED", "goal_id": str(row.id)}
    if action == "teleport":
        capsule = await task_capsule(session, row)
        try:
            from app.everywhere.inbox import push_inbox

            await push_inbox(
                session, device_id=device.id, kind="handoff_ready",
                title="Task moved", body=f"“{row.objective[:120]}” is ready here.",
                payload={"goal_id": str(row.id)},
            )
        except Exception:
            pass
        return {**base, "reply": "Moved — the full context is waiting on the other device.",
                "action_result": "COMPLETED", "goal_id": str(row.id),
                "capsule": capsule}
    if action == "answer_from_situation":
        command = str(resolution.get("command") or "")
        if command == ShortCommand.WHAT_CHANGED.value:
            delta = await what_changed(session)
            n = len(delta.get("changes") or [])
            return {**base, "reply": f"{n} meaningful change(s) since yesterday. Ask for detail on any of them.",
                    "action_result": "COMPLETED", "delta": delta}
        if command == ShortCommand.WHAT_NEEDS_ME:
            sit = await situation(session)
            approvals = sit.get("pending_approvals") or []
            if approvals:
                return {**base, "reply": f"{len(approvals)} thing(s) need you — oldest first: {(approvals[0].get('title') or 'approval')[:120]}.",
                        "action_result": "NEEDS_CONFIRMATION"}
            return {**base, "reply": "Nothing needs you right now.",
                    "action_result": "COMPLETED"}
        if command == ShortCommand.WHY_WAITING.value:
            if row.state in (GoalState.WAITING.value, GoalState.WAITING_FOR_CONDITION.value,
                             GoalState.WAITING_FOR_DEVICE.value, GoalState.WAITING_FOR_APPROVAL.value,
                             GoalState.STALLED.value):
                why = row.blocked_reason or str((row.next_condition or {}).get("expect") or "a condition")
                return {**base, "reply": f"We're waiting on: {why[:280]}.",
                        "action_result": "COMPLETED", "goal_id": str(row.id)}
            return {**base, "reply": f"That one isn't waiting — it's {row.state.lower()}.",
                    "action_result": "COMPLETED", "goal_id": str(row.id)}
    return {**base, "reply": "Working on it.", "action_result": "PARTIAL",
            "goal_id": str(row.id)}


# --- Shadow execution (pre-dispatch plan, never mutates) ---


def build_shadow_plan(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """Propose + collide + risk-check a mutation set. Pure and read-only."""
    mutating = [n for n in nodes if n.get("kind") in (
        "CORE_WRITE", "HOME_STATION_ACTION", "ARTIFACT_OPERATION", "CLOUD_JOB")]
    paths: list[str] = []
    for n in mutating:
        effect = str(n.get("expected_effect") or "")
        paths.extend(effect.split())
    seen: dict[str, int] = {}
    collisions: list[str] = []
    for p in paths:
        seen[p] = seen.get(p, 0) + 1
        if seen[p] == 2:
            collisions.append(p)
    risks = sorted({str(n.get("risk") or "R1") for n in mutating})
    need_confirm = [n.get("node_id") for n in mutating if str(n.get("risk") or "R1") in ("R3", "R4")]
    return {
        "mutated": False,
        "proposed_actions": len(mutating),
        "collisions": collisions[:16],
        "risks": risks,
        "required_confirmations": need_confirm[:16],
        "rollback": "Re-run prior graph version; terminal nodes are never re-executed.",
    }


# --- Derived work graph (materialized view, Core stays truth) ---


async def work_graph(session: AsyncSession, *, limit: int = 25) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    try:
        contracts = await list_contracts(session, limit=limit)
        for c in contracts:
            nodes.append({"id": f"contract:{c.id}", "type": "contract",
                          "label": c.objective[:80], "state": c.state})
            if c.linked_goal_id:
                edges.append({"from": f"contract:{c.id}", "to": f"goal:{c.linked_goal_id}",
                              "rel": "continued_from"})
    except Exception:
        pass
    try:
        from app.life.service import list_goals

        for g in (await list_goals(session, actor="master"))[:limit]:
            nodes.append({"id": f"goal:{g.get('id')}", "type": "goal",
                          "label": str(g.get("title") or "")[:80],
                          "state": g.get("state")})
            if g.get("project_id"):
                edges.append({"from": f"goal:{g.get('id')}",
                              "to": f"project:{g.get('project_id')}", "rel": "belongs_to"})
    except Exception:
        pass
    return {"nodes": nodes[:100], "edges": edges[:100], "derived": True}

async def mission_status(session: AsyncSession) -> dict[str, Any]:
    try:
        active = await list_contracts(session, states=[GoalState.ACTIVE.value], limit=10)
        waiting = await list_contracts(
            session,
            states=[GoalState.WAITING.value, GoalState.WAITING_FOR_CONDITION.value,
                    GoalState.WAITING_FOR_DEVICE.value, GoalState.WAITING_FOR_APPROVAL.value,
                    GoalState.STALLED.value],
            limit=10,
        )
        done = await list_contracts(session, states=[GoalState.COMPLETED.value], limit=5)
        parked = await list_contracts(session, states=[GoalState.PARKED.value], limit=5)
    except Exception:
        active, waiting, done, parked = [], [], [], []
    try:
        from app.everywhere.approvals import pending_approvals

        approvals = await pending_approvals(session, limit=5)
    except Exception:
        approvals = []
    focus = public_contract(active[0]) if active else None
    return {
        "now": focus,
        "working": [public_contract(c) for c in active[1:4]],
        "waiting": [public_contract(c) for c in waiting[:5]],
        "needs_you": approvals,
        "done_recently": [public_contract(c) for c in done[:5]],
        "parked": [public_contract(c) for c in parked[:5]],
        "pocket": {
            "working": len(active),
            "needs_you": len(approvals),
        },
    }
