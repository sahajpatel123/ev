"""Presence OS V1 — canonical graph runner (durable, deterministic, fail-closed).

Owns node dispatch for PresenceContract graphs. No signature changes to
sibling domains; every cross-domain import is lazy inside functions so this
module never hard-couples fleet law boundaries. Offline-hermetic: every
external effect (research, broker, spark, push) degrades to an honest
FAILED/WAITING node — never a faked SUCCEEDED.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PresenceCondition, PresenceContract
from app.presence import service as _service
from app.presence.contract import (
    TERMINAL_STATES,
    GoalState,
    can_transition,
)

_RISK_RANK = {"R1": 1, "R2": 2, "R3": 3, "R4": 4}

_TERMINAL_NODE = ("SUCCEEDED", "FAILED", "CANCELLED", "SKIPPED")
_NON_TERMINAL_BROKER = ("REQUESTED", "ROUTED", "QUEUED", "EXECUTING")

_WAITING_SCAN_STATES = (
    GoalState.WAITING.value,
    GoalState.WAITING_FOR_CONDITION.value,
    GoalState.WAITING_FOR_DEVICE.value,
    GoalState.WAITING_FOR_APPROVAL.value,
)

_TICK_CONDITION_CLASSES = (
    "TIME",
    "DEVICE_ONLINE",
    "DEVICE_OFFLINE",
    "TASK_COMPLETED",
    "APPROVAL_RESOLVED",
    "RESEARCH_RESULT",
)


def _tele(kind: str, **fields: Any) -> None:
    try:
        from app.device_gateway.telemetry import emit

        emit(kind, **fields)
    except Exception:
        pass


def _risk_rank(value: Any) -> int:
    return _RISK_RANK.get(str(value or "R1").upper()[:2], 1)


def _nodes(row: PresenceContract) -> list[dict[str, Any]]:
    g = row.graph if isinstance(row.graph, dict) else {}
    raw = g.get("nodes") or []
    return [dict(n) for n in raw if isinstance(n, dict)]


def _node_payload(node: dict[str, Any]) -> dict[str, Any]:
    for key in ("payload", "args", "params"):
        val = node.get(key)
        if isinstance(val, dict):
            return dict(val)
    return {}


def _definitive(nodes: list[dict[str, Any]]) -> bool:
    """True when every failure is definitive (proof-negative or structural).

    Transient executor errors (provider down, timeouts) stay retryable:
    the contract remains ACTIVE PARTIAL for operator retry.
    """
    for node in nodes:
        status = str(node.get("status"))
        if status in ("SUCCEEDED", "SKIPPED"):
            continue
        if status == "CANCELLED":
            continue
        reason = str(node.get("reason") or "")
        if not reason:
            stored = node.get("evidence")
            reason = stored if isinstance(stored, str) else ""
        if not (
            reason.startswith("verify:")
            or reason.startswith("unknown_kind")
            or reason.startswith("blocked:")
            or reason in ("artifact_error:no_content", "dep_failed")
        ):
            return False
    return True

def _runnable(nodes: list[dict[str, Any]]) -> dict[str, Any] | None:
    by_id = {str(n.get("node_id") or ""): n for n in nodes}
    for node in nodes:
        if str(node.get("status") or "PENDING") != "PENDING":
            continue
        deps = node.get("depends_on") or []
        blocked = False
        for dep in deps:
            dep_node = by_id.get(str(dep))
            if dep_node is None or str(dep_node.get("status")) != "SUCCEEDED":
                blocked = True
                break
        if not blocked:
            return node
    return None


def _poisoned(nodes: list[dict[str, Any]]) -> dict[str, Any] | None:
    by_id = {str(n.get("node_id") or ""): n for n in nodes}
    for node in nodes:
        if str(node.get("status") or "PENDING") != "PENDING":
            continue
        for dep in node.get("depends_on") or []:
            dep_node = by_id.get(str(dep))
            if dep_node is not None and str(dep_node.get("status")) in (
                "FAILED",
                "CANCELLED",
                "SKIPPED",
            ):
                return node
    return None
def _merge_evidence(row: PresenceContract, key: str, value: Any) -> None:
    merged = dict(row.evidence or {})
    merged[key] = value
    row.evidence = merged


def _storage_root() -> str:
    return os.environ.get("EV_STORAGE_ROOT", "./storage")


def _contract_dir(row: PresenceContract) -> str:
    return os.path.join(_storage_root(), "sandbox", "presence", str(row.id))


async def _set_wait_safe(
    session: AsyncSession,
    row: PresenceContract,
    wait_state: str,
    *,
    reason: str = "",
    condition: dict | None = None,
) -> None:
    if row.state == wait_state:
        if condition is not None:
            row.next_condition = dict(condition)
            await session.flush()
        return
    if can_transition(row.state, wait_state):
        await _service.set_wait(
            session, row, wait_state=wait_state, condition=condition, reason=reason
        )


async def _needs_you(
    session: AsyncSession, row: PresenceContract, *, title: str, body: str
) -> None:
    try:
        verdict = _service.attention_verdict(
            interruption=str(row.interruption_policy or "NORMAL"),
            priority=str(row.priority or "NORMAL"),
            approval_required=True,
        )
        await _service.deliver(
            session,
            row,
            title=title,
            body=body,
            verdict=verdict,
            payload={"needs_you": True},
        )
    except Exception:
        pass


# --- Node executors: each returns (completed_step, stop_loop, result) ---


async def _exec_wait_condition(
    session: AsyncSession, row: PresenceContract, node: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    cond_class = str(payload.get("cond_class") or "TIME")
    cond_payload = payload.get("condition")
    if not isinstance(cond_payload, dict):
        cond_payload = payload.get("payload")
        if not isinstance(cond_payload, dict):
            cond_payload = {}
    try:
        await _service.add_condition(
            session,
            row,
            cond_class=cond_class,
            payload=dict(cond_payload),
            frequency_s=int(payload.get("frequency_s") or 60),
            ttl_s=int(payload.get("ttl_s") or 86400),
        )
    except Exception as exc:
        return {"status": "FAILED", "reason": f"condition_rejected:{type(exc).__name__}"}
    await _set_wait_safe(
        session,
        row,
        GoalState.WAITING_FOR_CONDITION.value,
        reason=f"wait:{node.get('node_id')}:{cond_class}"[:500],
        condition={"expect": str(payload.get("expect") or cond_class)},
    )
    await _service.mark_node(session, row, str(node.get("node_id") or ""), "WAITING")
    return {"status": "WAITING", "wait": GoalState.WAITING_FOR_CONDITION.value}


async def _exec_wait_device(
    session: AsyncSession, row: PresenceContract, node: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    role = str(payload.get("role") or "home_station")
    await _service.add_condition(
        session,
        row,
        cond_class="DEVICE_ONLINE",
        payload={"role": role},
    )
    await _set_wait_safe(
        session,
        row,
        GoalState.WAITING_FOR_DEVICE.value,
        reason=f"wait-device:{role}"[:500],
        condition={"expect": f"device_online:{role}"},
    )
    await _service.mark_node(session, row, str(node.get("node_id") or ""), "WAITING")
    return {"status": "WAITING", "wait": GoalState.WAITING_FOR_DEVICE.value, "role": role}


async def _exec_wait_approval(
    session: AsyncSession, row: PresenceContract, node: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    await _set_wait_safe(
        session,
        row,
        GoalState.WAITING_FOR_APPROVAL.value,
        reason=str(payload.get("reason") or f"approval:{node.get('node_id')}")[:500],
        condition={"expect": str(payload.get("expect") or "owner_approval")},
    )
    await _service.mark_node(session, row, str(node.get("node_id") or ""), "WAITING")
    await _needs_you(
        session,
        row,
        title=str(payload.get("title") or f"Needs you: {row.objective[:80]}"),
        body=str(payload.get("body") or f"“{row.objective[:280]}” is waiting on your approval."),
    )
    return {"status": "WAITING", "wait": GoalState.WAITING_FOR_APPROVAL.value}


async def _exec_research(
    session: AsyncSession, row: PresenceContract, node: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    node_id = str(node.get("node_id") or "")
    try:
        from app.ev.research import ResearchService
        from app.schemas import ResearchJobCreate

        effect = str(node.get("expected_effect") or payload.get("effect") or "")
        goal = f"{row.objective} :: {effect}".strip(" :")[:2000] or row.objective
        svc = ResearchService(session, "master")
        job_row = await svc.create_job(ResearchJobCreate(goal=goal, allowed_tools=["web_search"]))
        job_id = job_row.id
        out = await svc.run_job(job_id)
    except Exception as exc:
        return {"status": "FAILED", "reason": f"research_error:{type(exc).__name__}"}
    status = str(out.get("status") or "")
    artifacts = list(out.get("final_artifacts") or [])
    citations = list(out.get("citations") or [])
    if status in ("completed", "concluded") and (artifacts or citations):
        _merge_evidence(
            row,
            f"research:{node_id}",
            {
                "job_id": str(job_id),
                "status": status,
                "artifact_count": len(artifacts),
                "citation_count": len(citations),
            },
        )
        await session.flush()
        return {"status": "SUCCEEDED", "job_id": str(job_id)}
    reason = str(out.get("last_error") or status or "research_incomplete")[:200]
    _merge_evidence(
        row, f"research:{node_id}", {"job_id": str(job_id), "status": status, "error": reason}
    )
    await session.flush()
    return {"status": "FAILED", "reason": reason, "job_id": str(job_id)}


async def _exec_home_station_action(
    session: AsyncSession, row: PresenceContract, node: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    from app.models import Device

    node_id = str(node.get("node_id") or "")
    requesting = None
    if row.origin_device_id is not None:
        requesting = await session.get(Device, row.origin_device_id)
    if requesting is None:
        return {"status": "FAILED", "reason": "blocked:no_requesting_device", "blocked": True}
    capability = str(payload.get("capability") or "device.ping")
    args = payload.get("args")
    if not isinstance(args, dict):
        args = payload.get("arguments")
        if not isinstance(args, dict):
            args = {}
    action_id = f"presence-{row.id}-{node_id}"[:128]
    try:
        from app.everywhere.device_actions import create_routed_action

        result = await create_routed_action(
            session,
            requesting_device=requesting,
            capability=capability,
            arguments=dict(args),
            action_id=action_id,
        )
    except Exception as exc:
        return {"status": "FAILED", "reason": f"broker_error:{type(exc).__name__}"}
    if str(result.get("error_code") or "") == "TARGET_DEVICE_OFFLINE":
        role = str(payload.get("role") or "home_station")
        with contextlib.suppress(Exception):
            await _service.add_condition(
                session,
                row,
                cond_class="DEVICE_ONLINE",
                payload={"role": role},
            )
        await _set_wait_safe(
            session,
            row,
            GoalState.WAITING_FOR_DEVICE.value,
            reason=f"target_offline:{capability}"[:500],
            condition={"expect": f"device_online:{role}"},
        )
        await _service.mark_node(session, row, node_id, "WAITING")
        return {
            "status": "WAITING",
            "wait": GoalState.WAITING_FOR_DEVICE.value,
            "reason": "TARGET_DEVICE_OFFLINE",
            "stop": True,
        }
    if result.get("ok") is True:
        _merge_evidence(
            row,
            f"broker:{node_id}",
            {
                "action_id": action_id,
                "capability": result.get("capability") or capability,
                "status": result.get("status"),
            },
        )
        await session.flush()
        return {"status": "SUCCEEDED", "action_id": action_id}
    return {"status": "FAILED", "reason": str(result.get("error_code") or "broker_rejected")[:200]}


async def _research_report(session: AsyncSession, row: PresenceContract, node_id: str) -> str:
    """Deterministic report over a completed RESEARCH node's evidence.

    No model call: renders stored notes/citations into a bounded markdown
    document with goal provenance. Synthesis wording stays factual.
    """
    ev = dict(row.evidence or {}).get(f"research:{node_id.strip()}", {})
    job_id = str((ev if isinstance(ev, dict) else {}).get("job_id") or "")
    notes: list[dict[str, Any]] = []
    cites: list[dict[str, Any]] = []
    try:
        if job_id:
            from uuid import UUID as _UUID

            from app.ev.research import ResearchService, list_notes

            for note in await list_notes(session, _UUID(job_id)):
                title = getattr(note, "title", "") or ""
                snippet = getattr(note, "snippet", "") or getattr(note, "content", "") or ""
                url = getattr(note, "url", "") or ""
                notes.append({"title": str(title)[:200], "snippet": str(snippet)[:800],
                              "url": str(url)[:500]})
            svc = ResearchService(session, "master")
            job = await svc.detail(_UUID(job_id))
            for cite in list(getattr(job, "citations", None) or []):
                if isinstance(cite, dict):
                    cites.append({"title": str(cite.get("title") or "")[:200],
                                  "snippet": str(cite.get("snippet") or "")[:800],
                                  "url": str(cite.get("url") or "")[:500]})
            artifacts = list(getattr(job, "final_artifacts", None) or [])
        else:
            artifacts = []
    except Exception:
        notes = []
        cites = []
        artifacts = []
    sources = notes + [c for c in cites if c not in notes]
    lines = [
        f"# Research report — {row.objective[:160]}",
        "",
        f"Goal: {row.id} (contract evidence, {len(sources)} source(s)).",
        "",
    ]
    for item in sources[:12]:
        lines.append(f"## {item['title'] or 'Finding'}")
        if item["snippet"]:
            lines.append(str(item["snippet"]))
        if item["url"]:
            lines.append(f"Source: {item['url']}")
        lines.append("")
    if artifacts:
        lines.append(f"Stored research artifacts: {len(artifacts)}.")
        lines.append("")
    if not sources:
        lines.append("No citable sources were stored for this goal.")
        lines.append("")
    return "\n".join(lines)[:20000]

async def _exec_artifact(
    session: AsyncSession, row: PresenceContract, node: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    node_id = str(node.get("node_id") or "")
    raw_name = str(payload.get("filename") or payload.get("path") or f"{node_id}.txt")
    filename = os.path.basename(raw_name)[:120] or f"{node_id}.txt"
    content: Any = payload.get("content", "")
    source = str(payload.get("content_source") or "")
    if not content and source.startswith("research_evidence:"):
        content = await _research_report(session, row, source.split(":", 1)[1])
    if not content:
        # Single completed RESEARCH node in this graph is the unambiguous
        # source; multiple or zero is ambiguous and fails honestly.
        done = [n for n in _nodes(row) if n.get("kind") == "RESEARCH"
                and str(n.get("status")) == "SUCCEEDED"]
        if len(done) == 1:
            content = await _research_report(session, row, str(done[0].get("node_id") or ""))
        else:
            return {"status": "FAILED", "reason": "artifact_error:no_content"}
    if isinstance(content, dict):
        blob = json.dumps(content, sort_keys=True).encode("utf-8")
    elif isinstance(content, bytes):
        blob = content
    else:
        blob = str(content).encode("utf-8")
    blob = blob[:1_000_000]
    digest = hashlib.sha256(blob).hexdigest()
    try:
        directory = _contract_dir(row)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, filename)
        existing: bytes | None = None
        if os.path.exists(path):
            with open(path, "rb") as fh:
                existing = fh.read()
        if existing != blob:
            with open(path, "wb") as fh:
                fh.write(blob)
        _merge_evidence(
            row,
            f"artifact:{node_id}",
            {
                "filename": filename,
                "sha256": digest,
                "bytes": len(blob),
            },
        )
        artifacts = dict(row.artifacts or {})
        artifacts[filename] = {"sha256": digest, "node": node_id}
        row.artifacts = artifacts
        await session.flush()
    except Exception as exc:
        return {"status": "FAILED", "reason": f"artifact_error:{type(exc).__name__}"}
    return {"status": "SUCCEEDED", "filename": filename, "sha256": digest}


async def _exec_verify(
    session: AsyncSession, row: PresenceContract, node: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    node_id = str(node.get("node_id") or "")
    predicate = str(payload.get("predicate") or payload.get("verify") or "")
    if predicate == "artifact_exists":
        filename = os.path.basename(str(payload.get("filename") or payload.get("path") or ""))[:120]
        if not filename:
            return {"status": "FAILED", "reason": "verify:no_filename"}
        path = os.path.join(_contract_dir(row), filename)
        if not os.path.exists(path):
            return {"status": "FAILED", "reason": "verify:artifact_missing"}
        if payload.get("sha256"):
            with open(path, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
            if digest != str(payload.get("sha256")):
                return {"status": "FAILED", "reason": "verify:sha_mismatch"}
            _merge_evidence(row, f"verify:{node_id}", {"predicate": predicate, "sha256": digest})
            await session.flush()
            return {"status": "SUCCEEDED", "sha256": digest}
        _merge_evidence(row, f"verify:{node_id}", {"predicate": predicate, "filename": filename})
        await session.flush()
        return {"status": "SUCCEEDED", "filename": filename}
    if predicate == "broker_succeeded":
        action_id = str(payload.get("action_id") or "")
        if not action_id:
            ref = str(payload.get("node_ref") or payload.get("node") or node_id)
            action_id = f"presence-{row.id}-{ref}"[:128]
        try:
            from app.everywhere.device_actions import get_action

            action_row = await get_action(session, action_id, owner_scope="master")
        except Exception as exc:
            return {"status": "FAILED", "reason": f"verify:broker_error:{type(exc).__name__}"}
        if action_row is not None and str(action_row.status) == "SUCCEEDED":
            _merge_evidence(
                row, f"verify:{node_id}", {"predicate": predicate, "action_id": action_id}
            )
            await session.flush()
            return {"status": "SUCCEEDED", "action_id": action_id}
        return {"status": "FAILED", "reason": "verify:broker_not_succeeded"}
    if predicate == "research_completed":
        job_ref = str(payload.get("job_id") or payload.get("ref") or "")
        if not job_ref:
            return {"status": "FAILED", "reason": "verify:no_job_ref"}
        try:
            from app.ev.research import ResearchService

            svc = ResearchService(session, "master")
            job = await svc.detail(UUID(job_ref))
        except Exception as exc:
            return {"status": "FAILED", "reason": f"verify:research_error:{type(exc).__name__}"}
        if job is not None and str(job.status or "") in ("completed", "concluded"):
            _merge_evidence(row, f"verify:{node_id}", {"predicate": predicate, "job_id": job_ref})
            await session.flush()
            return {"status": "SUCCEEDED", "job_id": job_ref}
        return {"status": "FAILED", "reason": "verify:research_incomplete"}
    if predicate == "contract_completed":
        target = payload.get("contract_id") or str(row.id)
        target_row = await _service.get_contract(session, target)
        if target_row is not None and str(target_row.state) == GoalState.COMPLETED.value:
            _merge_evidence(row, f"verify:{node_id}", {"predicate": predicate})
            await session.flush()
            return {"status": "SUCCEEDED"}
        return {"status": "FAILED", "reason": "verify:contract_incomplete"}
    return {"status": "FAILED", "reason": f"verify:unknown_predicate:{predicate[:60]}"}


async def _exec_notify(
    session: AsyncSession, row: PresenceContract, node: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    node_id = str(node.get("node_id") or "")
    title = str(payload.get("title") or f"Update: {row.objective[:80]}")[:160]
    body = str(payload.get("body") or row.objective[:280])[:1900]
    try:
        out = await _service.deliver(session, row, title=title, body=body)
    except Exception as exc:
        return {"status": "FAILED", "reason": f"notify_error:{type(exc).__name__}"}
    _merge_evidence(
        row,
        f"notify:{node_id}",
        {
            "verdict": out.get("verdict"),
            "delivered": bool(out.get("delivered")),
        },
    )
    await session.flush()
    return {"status": "SUCCEEDED", "verdict": out.get("verdict")}


async def _exec_handoff(
    session: AsyncSession, row: PresenceContract, node: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    node_id = str(node.get("node_id") or "")
    if row.origin_device_id is None:
        return {"status": "FAILED", "reason": "blocked:no_origin_device", "blocked": True}
    try:
        from app.everywhere.inbox import push_inbox

        capsule = await _service.task_capsule(session, row)
        item = await push_inbox(
            session,
            device_id=row.origin_device_id,
            kind="handoff_ready",
            title=str(payload.get("title") or "Task moved")[:160],
            body=str(payload.get("body") or f"“{row.objective[:120]}” is ready here.")[:2000],
            payload={"goal_id": str(row.id), "capsule_version": capsule.get("version", 0)},
        )
        _merge_evidence(row, f"handoff:{node_id}", {"item_id": str(getattr(item, "id", "") or "")})
        await session.flush()
        return {"status": "SUCCEEDED", "item_id": str(getattr(item, "id", "") or "")}
    except Exception as exc:
        return {"status": "FAILED", "reason": f"handoff_error:{type(exc).__name__}"}

async def _exec_core_read(
    session: AsyncSession, row: PresenceContract, node: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    node_id = str(node.get("node_id") or "")
    read = str(payload.get("read") or "situation")
    try:
        if read == "situation":
            data = await _service.situation(session)
        elif read == "what_changed":
            data = await _service.what_changed(session)
        elif read == "mission":
            data = await _service.mission_status(session)
        else:
            return {"status": "FAILED", "reason": f"unknown_read:{read[:60]}"}
    except Exception as exc:
        return {"status": "FAILED", "reason": f"core_read_error:{type(exc).__name__}"}
    try:
        blob = json.dumps(data, default=str)[:20_000]
        _merge_evidence(row, f"core_read:{node_id}", {"read": read, "snapshot": json.loads(blob)})
    except Exception:
        _merge_evidence(row, f"core_read:{node_id}", {"read": read})
    await session.flush()
    return {"status": "SUCCEEDED", "read": read}


async def _exec_core_write(
    session: AsyncSession, row: PresenceContract, node: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    node_id = str(node.get("node_id") or "")
    if row.linked_goal_id is None:
        return {"status": "FAILED", "reason": "blocked:no_linked_goal", "blocked": True}
    try:
        from app.models import Goal

        goal = await session.get(Goal, row.linked_goal_id)
        if goal is None:
            return {"status": "FAILED", "reason": "blocked:goal_missing", "blocked": True}
        note = payload.get("progress_note")
        nxt = payload.get("next_action")
        if note is None and nxt is None:
            return {"status": "FAILED", "reason": "blocked:nothing_to_write", "blocked": True}
        if note is not None:
            goal.progress_note = str(note)[:2000]
        if nxt is not None:
            goal.next_action = str(nxt)[:2000]
        await session.flush()
    except Exception as exc:
        return {"status": "FAILED", "reason": f"core_write_error:{type(exc).__name__}"}
    _merge_evidence(row, f"core_write:{node_id}", {"goal_id": str(row.linked_goal_id)})
    await session.flush()
    return {"status": "SUCCEEDED"}


async def _exec_spark(
    session: AsyncSession, row: PresenceContract, node: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """Reason over the graph remainder via the whole-contract compiler.

    The compiler owns whole-graph compilation (session, contract) — there is
    no per-node entrypoint, so a SPARK_REASONING node triggers one bounded
    recompile of the remainder for evidence only. The runner never merges
    compiler output mid-run; dispatch stays with the stored graph.
    """
    node_id = str(node.get("node_id") or "")
    try:
        from app.presence.compiler import compile_graph
    except Exception:
        return {"status": "FAILED", "reason": "spark_unavailable"}
    try:
        remainder = [
            {
                "node_id": n.get("node_id"),
                "kind": n.get("kind"),
                "status": n.get("status"),
                "expected_effect": n.get("expected_effect"),
            }
            for n in _nodes(row)
            if str(n.get("status")) in ("PENDING", "RUNNING", "WAITING")
        ][:16]
        out = await compile_graph(
            session,
            row,
            context={"node_id": node_id, "remainder": remainder},
            budget_s=60,
            persist=False,
        )
    except Exception as exc:
        if type(exc).__name__ in ("SparkUnavailable", "MuseProviderUnavailable"):
            return {"status": "FAILED", "reason": "spark_unavailable"}
        return {"status": "FAILED", "reason": f"spark_error:{type(exc).__name__}"}
    compiled = out.get("nodes") if isinstance(out, dict) else None
    if isinstance(compiled, list) and compiled:
        _merge_evidence(
            row,
            f"spark:{node_id}",
            {
                "compiled_nodes": len(compiled),
                "warnings": list(out.get("warnings") or [])[:8],
                "model": str(out.get("model") or ""),
            },
        )
        await session.flush()
        return {"status": "SUCCEEDED", "compiled_nodes": len(compiled)}
    return {"status": "FAILED", "reason": "spark_empty_result"}


async def _exec_cloud_job(
    session: AsyncSession, row: PresenceContract, node: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    tool = str(payload.get("tool") or "web_search")
    if tool not in ("web_search",):
        return {
            "status": "FAILED",
            "reason": f"blocked:unsupported_tool:{tool[:40]}",
            "blocked": True,
        }
    out = await _exec_research(session, row, node, payload)
    if out.get("status") == "SUCCEEDED":
        _merge_evidence(
            row,
            f"cloud_job:{node.get('node_id')}",
            {"via": "research", "job_id": out.get("job_id")},
        )
        await session.flush()
    return out


async def _exec_phone_local(
    session: AsyncSession, row: PresenceContract, node: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    node_id = str(node.get("node_id") or "")
    if row.origin_device_id is None:
        return {"status": "FAILED", "reason": "blocked:no_origin_device", "blocked": True}
    try:
        from app.everywhere.inbox import push_inbox

        item = await push_inbox(
            session,
            device_id=row.origin_device_id,
            kind="task",
            title=str(payload.get("title") or f"On-device task: {row.objective[:80]}")[:160],
            body=str(payload.get("body") or f"“{row.objective[:280]}” needs its on-device step.")[
                :2000
            ],
            payload={"goal_id": str(row.id), "node_id": node_id},
        )
    except Exception as exc:
        return {"status": "FAILED", "reason": f"phone_task_error:{type(exc).__name__}"}
    item_id = item.get("id") if isinstance(item, dict) else getattr(item, "id", None)
    _merge_evidence(row, f"phone_task:{node_id}", {"inbox_item_id": str(item_id or "")})
    await _service.mark_node(session, row, node_id, "WAITING")
    await session.flush()
    return {"status": "WAITING", "inbox_item_id": str(item_id or "")}


async def _exec_digital(
    session: AsyncSession, row: PresenceContract, node: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    from app.digital.presence_exec import exec_digital_node

    return await exec_digital_node(session, row, node, payload)


_EXECUTORS = {
    "WAIT_CONDITION": _exec_wait_condition,
    "WAIT_DEVICE": _exec_wait_device,
    "WAIT_APPROVAL": _exec_wait_approval,
    "RESEARCH": _exec_research,
    "HOME_STATION_ACTION": _exec_home_station_action,
    "ARTIFACT_OPERATION": _exec_artifact,
    "VERIFY": _exec_verify,
    "NOTIFY": _exec_notify,
    "HANDOFF": _exec_handoff,
    "CORE_READ": _exec_core_read,
    "CORE_WRITE": _exec_core_write,
    "SPARK_REASONING": _exec_spark,
    "CLOUD_JOB": _exec_cloud_job,
    "PHONE_LOCAL_ACTION": _exec_phone_local,
    "EMAIL_READ": _exec_digital,
    "EMAIL_SEND": _exec_digital,
    "WHATSAPP_READ": _exec_digital,
    "WHATSAPP_SEND": _exec_digital,
    "CONTACT_RESOLVE": _exec_digital,
    "CALENDAR_CREATE": _exec_digital,
    "BROWSER_ACTION": _exec_digital,
    "ARTIFACT_DOWNLOAD": _exec_digital,
    "COMMUNICATION_WAIT": _exec_digital,
}


async def advance(
    session: AsyncSession, contract_id: Any, *, max_steps: int = 16
) -> dict[str, Any]:
    """Run runnable PENDING nodes until waiting, exhausted, or bounded."""
    row = await _service.get_contract(session, contract_id)
    if row is None:
        return {"advanced": False, "node_results": {}, "state": None, "reason": "not_found"}
    if row.state in (GoalState.PARKED.value, GoalState.CANCELLED.value) or row.state in {
        s.value for s in TERMINAL_STATES
    }:
        return {
            "advanced": False,
            "node_results": {},
            "state": row.state,
            "reason": "terminal_or_parked",
        }
    ceiling = row.risk_ceiling or "R2"
    node_results: dict[str, Any] = {}
    steps = 0
    stop = False
    budget = max(1, min(int(max_steps or 16), 64))
    while steps < budget and not stop:
        nodes = _nodes(row)
        poison = _poisoned(nodes)
        if poison is not None:
            pid = str(poison.get("node_id") or "")
            await _service.mark_node(session, row, pid, "SKIPPED", evidence="dep_failed")
            node_results[pid] = {"status": "SKIPPED", "reason": "dep_failed"}
            _tele("presence.node", goal_id=str(row.id), node=pid, status="SKIPPED")
            steps += 1
            continue
        node = _runnable(nodes)
        if node is None:
            break
        node_id = str(node.get("node_id") or "")
        risk = str(node.get("risk") or "R1")
        if _risk_rank(risk) > _risk_rank(ceiling):
            await _set_wait_safe(
                session,
                row,
                GoalState.WAITING_FOR_APPROVAL.value,
                reason=f"risk {risk} above ceiling {ceiling}"[:500],
                condition={"expect": "owner_approval"},
            )
            await _service.mark_node(session, row, node_id, "WAITING")
            await _needs_you(
                session,
                row,
                title=f"Needs you: {row.objective[:80]}",
                body=f"“{row.objective[:280]}” needs approval (risk {risk} above ceiling {ceiling}).",
            )
            node_results[node_id] = {"status": "WAITING", "reason": "risk_above_ceiling"}
            _tele("presence.risk_gate", goal_id=str(row.id), node=node_id, risk=risk)
            stop = True
            break
        await _service.mark_node(session, row, node_id, "RUNNING")
        payload = _node_payload(node)
        executor = _EXECUTORS.get(str(node.get("kind") or ""))
        if executor is None:
            await _service.mark_node(session, row, node_id, "FAILED", evidence="unknown_kind")
            node_results[node_id] = {"status": "FAILED", "reason": "unknown_kind"}
            _tele("presence.node", goal_id=str(row.id), node=node_id, status="FAILED")
            steps += 1
            continue
        try:
            result = await executor(session, row, node, payload)
        except Exception as exc:
            result = {"status": "FAILED", "reason": f"executor_error:{type(exc).__name__}"}
        status = str(result.get("status") or "FAILED")
        if status == "SUCCEEDED":
            await _service.mark_node(session, row, node_id, "SUCCEEDED")
            node_results[node_id] = result
        elif status == "WAITING":
            # Executor already parked the node when a wait was entered.
            fresh = [n for n in _nodes(row) if str(n.get("node_id")) == node_id]
            if fresh and str(fresh[0].get("status")) == "RUNNING":
                await _service.mark_node(session, row, node_id, "WAITING")
            node_results[node_id] = result
            if result.get("stop", True):
                stop = True
        else:
            await _service.mark_node(
                session,
                row,
                node_id,
                "FAILED",
                evidence=str(result.get("reason") or "failed")[:500],
            )
            node_results[node_id] = result
        _tele("presence.node", goal_id=str(row.id), node=node_id, status=status)
        steps += 1
    completion = "PARTIAL"
    nodes = _nodes(row)
    if (
        nodes
        and all(str(n.get("status")) == "SUCCEEDED" for n in nodes)
        and dict(row.evidence or {})
        and can_transition(row.state, GoalState.COMPLETED.value)
    ):
        await _service.transition(
            session,
            row,
            GoalState.COMPLETED.value,
            reason="graph_complete",
            confidence="COMPLETED_VERIFIED",
        )
        completion = "COMPLETED_VERIFIED"
    elif (
        nodes
        and all(str(n.get("status")) in ("SUCCEEDED", "FAILED", "CANCELLED", "SKIPPED") for n in nodes)
        and any(str(n.get("status")) in ("FAILED", "CANCELLED") for n in nodes)
        and _definitive(nodes)
        and can_transition(row.state, GoalState.FAILED.value)
    ):
        # Graph exhausted with definitive failures and nothing left to wait
        # on: fail honestly (partial evidence kept) instead of stranding
        # ACTIVE. Transient executor errors stay ACTIVE for operator retry.
        await _service.transition(
            session,
            row,
            GoalState.FAILED.value,
            reason="graph_exhausted_with_failures",
            confidence="COMPLETED_PARTIAL",
        )
        completion = "COMPLETED_PARTIAL"
    await session.flush()
    return {
        "advanced": bool(node_results),
        "node_results": node_results,
        "state": row.state,
        "steps": steps,
        "completion": completion,
        "contract_id": str(row.id),
    }


async def cancel_contract(
    session: AsyncSession, row: PresenceContract, reason: str
) -> dict[str, Any]:
    """Fail-closed cancel: contract, nodes, conditions, jobs, broker rows."""
    summary: dict[str, Any] = {
        "cancelled": True,
        "state": row.state,
        "nodes": 0,
        "conditions": 0,
        "actions": 0,
        "jobs": 0,
    }
    if can_transition(row.state, GoalState.CANCELLED.value):
        await _service.transition(
            session, row, GoalState.CANCELLED.value, reason=(reason or "cancelled")[:1000]
        )
    for node in _nodes(row):
        if str(node.get("status")) in ("PENDING", "RUNNING", "WAITING"):
            await _service.mark_node(session, row, str(node.get("node_id") or ""), "CANCELLED")
            summary["nodes"] += 1
    conds = list(
        (
            await session.execute(
                select(PresenceCondition).where(
                    PresenceCondition.contract_id == row.id,
                    PresenceCondition.state == "PENDING",
                )
            )
        )
        .scalars()
        .all()
    )
    for cond in conds:
        cond.state = "CANCELLED"
        summary["conditions"] += 1
    await session.flush()
    # Best-effort: research jobs referenced from evidence.
    for value in dict(row.evidence or {}).values():
        job_ref = ""
        if isinstance(value, dict):
            job_ref = str(value.get("job_id") or "")
        elif isinstance(value, str):
            job_ref = value
        if not job_ref:
            continue
        try:
            from app.ev.research import ResearchService

            svc = ResearchService(session, "master")
            await svc.cancel_job(UUID(job_ref))
            summary["jobs"] += 1
        except Exception:
            continue
    # Best-effort: broker rows created by this contract's nodes.
    try:
        from app.models import DeviceRoutedAction

        prefix = f"presence-{row.id}-"
        rows = list(
            (
                await session.execute(
                    select(DeviceRoutedAction).where(
                        DeviceRoutedAction.action_id.like(f"{prefix}%")
                    )
                )
            )
            .scalars()
            .all()
        )
        for action_row in rows:
            if str(action_row.status) in _NON_TERMINAL_BROKER:
                action_row.status = "CANCELLED"
                summary["actions"] += 1
        await session.flush()
    except Exception:
        pass
    summary["state"] = row.state
    _tele("presence.cancel", goal_id=str(row.id), reason=(reason or "")[:120])
    return summary


async def detect_stalls(session: AsyncSession, *, older_than_s: int = 86400) -> list[str]:
    """Stall waiting contracts whose wait has no live condition."""
    try:
        threshold = max(0, int(older_than_s))
    except (TypeError, ValueError):
        threshold = 86400
    now = datetime.now(UTC)
    rows = list(
        (
            await session.execute(
                select(PresenceContract).where(PresenceContract.state.in_(_WAITING_SCAN_STATES))
            )
        )
        .scalars()
        .all()
    )
    stalled: list[str] = []
    for row in rows:
        updated = row.updated_at
        if updated is not None:
            aware = updated if updated.tzinfo else updated.replace(tzinfo=UTC)
            if (now - aware).total_seconds() < threshold:
                continue
        pending = (
            (
                await session.execute(
                    select(PresenceCondition).where(
                        PresenceCondition.contract_id == row.id,
                        PresenceCondition.state == "PENDING",
                    )
                )
            )
            .scalars()
            .all()
        )
        if len(list(pending)) > 0:
            continue
        if not can_transition(row.state, GoalState.STALLED.value):
            continue
        await _service.transition(
            session, row, GoalState.STALLED.value, reason="wait_without_live_condition"
        )
        with contextlib.suppress(Exception):
            await _service.deliver(
                session,
                row,
                title=f"Stalled: {row.objective[:80]}",
                body=f"“{row.objective[:280]}” is stalled — its wait has no live condition.",
            )
        _tele("presence.stall", goal_id=str(row.id))
        stalled.append(str(row.id))
    await session.flush()
    return stalled


def _condition_due(cond: PresenceCondition, now: datetime) -> bool:
    if str(cond.state) != "PENDING":
        return False
    created = cond.created_at
    if created is not None:
        aware = created if created.tzinfo else created.replace(tzinfo=UTC)
        if (now - aware).total_seconds() > int(cond.ttl_s or 86400):
            return True  # evaluate once so expiry is recorded
    last = cond.last_evaluated_at
    if last is None:
        return True
    aware_last = last if last.tzinfo else last.replace(tzinfo=UTC)
    return (now - aware_last).total_seconds() >= int(cond.frequency_s or 60)


async def presence_tick(session: AsyncSession, now: datetime, *, limit: int = 25) -> dict[str, Any]:
    """Bounded scheduler pass: evaluate due conditions, resume, advance, stalls."""
    counts: dict[str, Any] = {
        "evaluated": 0,
        "resumed": 0,
        "advanced": 0,
        "stalled": [],
    }
    try:
        lim = max(1, min(int(limit or 25), 100))
    except (TypeError, ValueError):
        lim = 25
    aware_now = now if now.tzinfo else now.replace(tzinfo=UTC)
    try:
        rows = list(
            (
                await session.execute(
                    select(PresenceContract)
                    .where(
                        PresenceContract.state.in_(
                            (GoalState.ACTIVE.value, *_WAITING_SCAN_STATES, GoalState.STALLED.value)
                        )
                    )
                    .order_by(PresenceContract.updated_at.asc())
                    .limit(lim)
                )
            )
            .scalars()
            .all()
        )
    except Exception:
        return counts
    for row in rows:
        try:
            if row.state in (*_WAITING_SCAN_STATES, GoalState.STALLED.value):
                conds = list(
                    (
                        await session.execute(
                            select(PresenceCondition).where(
                                PresenceCondition.contract_id == row.id,
                                PresenceCondition.state == "PENDING",
                                PresenceCondition.cond_class.in_(_TICK_CONDITION_CLASSES),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                satisfied_now = False
                for cond in conds:
                    if not _condition_due(cond, aware_now):
                        continue
                    try:
                        before = cond.state
                        await _service.evaluate_condition(session, cond)
                        counts["evaluated"] += 1
                        if before == "PENDING" and cond.state == "SATISFIED":
                            satisfied_now = True
                    except Exception:
                        continue
                # NOTE: resume_if_ready only sees PENDING rows, but this tick
                # just satisfied some — so resume is decided here from the
                # post-evaluation states, then the contract advances below.
                state_now = row.state
                if satisfied_now and state_now in (*_WAITING_SCAN_STATES, GoalState.STALLED.value):
                    try:
                        await _service.transition(session, row, GoalState.ACTIVE.value, reason="")
                        await _service._emit(
                            session,
                            "goal.condition_satisfied",
                            {"goal_id": str(row.id), "via": "tick"},
                            row.origin_device_id,
                        )
                        state_now = GoalState.ACTIVE.value
                        counts["resumed"] += 1
                    except Exception:
                        pass
            else:
                state_now = row.state
            if state_now == GoalState.ACTIVE.value:
                out = await advance(session, row.id)
                if out.get("advanced"):
                    counts["advanced"] += 1
        except Exception:
            continue
    try:
        counts["stalled"] = await detect_stalls(session)
    except Exception:
        counts["stalled"] = []
    return counts


async def consider_event(
    session: AsyncSession, event_type: str, content: dict[str, Any] | None
) -> dict[str, Any]:
    """Event-driven resume: evaluate matching PENDING conditions, resume+advance once."""
    counts: dict[str, Any] = {"evaluated": 0, "satisfied": 0, "resumed": 0, "advanced": 0}
    et = str(event_type or "")
    if et.startswith("goal."):
        counts["ignored"] = True
        return counts
    body = dict(content or {})
    classes: tuple[str, ...]
    if et.startswith("research."):
        classes = ("RESEARCH_RESULT", "TASK_COMPLETED")
    elif et.startswith("approval"):
        classes = ("APPROVAL_RESOLVED",)
    elif et.startswith("device"):
        classes = ("DEVICE_ONLINE", "DEVICE_OFFLINE")
    elif et.startswith("presence"):
        classes = _TICK_CONDITION_CLASSES
    else:
        counts["ignored"] = True
        return counts
    refs = {
        str(body.get(k) or "")
        for k in ("job_id", "session_id", "action_id", "ref", "role", "contract_id", "goal_id")
    }
    refs.discard("")
    query = select(PresenceCondition).where(
        PresenceCondition.state == "PENDING",
        PresenceCondition.cond_class.in_(classes),
    )
    contract_ref = str(body.get("contract_id") or body.get("goal_id") or "")
    if contract_ref:
        with contextlib.suppress(ValueError, TypeError, AttributeError):
            query = query.where(PresenceCondition.contract_id == UUID(contract_ref))
    try:
        conds = list((await session.execute(query.limit(25))).scalars().all())
    except Exception:
        return counts
    touched: set[str] = set()
    for cond in conds:
        if refs:
            payload = dict(cond.payload or {})
            hay = {str(v) for v in payload.values()}
            if refs.isdisjoint(hay):
                continue
        try:
            satisfied = await _service.evaluate_condition(session, cond)
            counts["evaluated"] += 1
        except Exception:
            continue
        if satisfied:
            counts["satisfied"] += 1
        touched.add(str(cond.contract_id))
    for cid in sorted(touched):
        try:
            row = await _service.get_contract(session, cid)
            if row is None:
                continue
            if await _service.resume_if_ready(session, row):
                counts["resumed"] += 1
            if row.state == GoalState.ACTIVE.value:
                out = await advance(session, row.id)
                if out.get("advanced"):
                    counts["advanced"] += 1
        except Exception:
            continue
    return counts
