"""Presence OS V1 — Spark structured graph compiler.

Turns a durable intent contract into a bounded, validated work graph. The
planner (Muse Spark) only *proposes* nodes; it never authorizes execution and
never claims success. Every proposed node is validated against the
capability-bounded vocabulary, checked against the contract risk ceiling, and
persisted via the presence service. Invalid nodes are rejected with warnings;
over-ceiling nodes are kept (with warnings), never silently dropped.

Fail-closed: missing key raises SparkUnavailable before any network attempt;
unparseable provider output raises instead of persisting guesses.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PresenceContract
from app.presence.contract import NodeKind, NodeTarget

logger = logging.getLogger("ev.presence.compiler")

# NOTE: the upstream structured-output validator rejects large enum sets,
# so kind/target stay plain strings here. _validate() below enforces the
# closed vocabularies (NodeKind/NodeTarget) and warns on violations.
GRAPH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "nodes": {
            "type": "array",
            "maxItems": 32,
            "items": {
                "type": "object",
                "properties": {
                    "node_id": {"type": "string"},
                    "kind": {"type": "string"},
                    "target": {"type": "string"},
                    "effect": {"type": "string"},
                    "risk": {"type": "string"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "verification": {"type": "string"},
                },
                "required": ["node_id", "kind", "target"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["nodes"],
    "additionalProperties": False,
}

_SPARK_SYSTEM = """You are the Presence work-graph planner (Muse Spark) for a trusted owner device.

Capability boundary — you may ONLY use these node kinds:
CORE_READ, CORE_WRITE, SPARK_REASONING, RESEARCH, CLOUD_JOB,
HOME_STATION_ACTION, PHONE_LOCAL_ACTION, WAIT_CONDITION, WAIT_DEVICE,
WAIT_APPROVAL, VERIFY, NOTIFY, HANDOFF, ARTIFACT_OPERATION,
EMAIL_READ, EMAIL_SEND, WHATSAPP_READ, WHATSAPP_SEND, CONTACT_RESOLVE,
CALENDAR_CREATE, BROWSER_ACTION, ARTIFACT_DOWNLOAD, COMMUNICATION_WAIT.
Targets are limited to: CORE, CLOUD, HOME_STATION, POCKET_NODE, SATELLITE_NODE.
Risk is one of R0, R1, R2, R3, R4. depends_on lists node_ids from this same plan.

Rules:
- Respond with JSON ONLY: {"nodes": [{"node_id", "kind", "target", "effect", "risk", "depends_on", "verification"}]}.
- You PROPOSE work; you never authorize execution and never claim success.
- Do not promise outcomes, do not report anything as done, do not invent
  capabilities, devices, or evidence beyond the context given.
- Keep the plan small (at most ~8 nodes). Prefer WAIT_* and VERIFY nodes for
  anything uncertain. High-risk effects (R3/R4) need a VERIFY node.
- VERIFY nodes must set verification to exactly: artifact_exists:<filename>
  (e.g. "artifact_exists:report.md"). Any other VERIFY text fails honestly
  at runtime — never claim success in prose.
- ARTIFACT_OPERATION nodes: put the desired filename in effect as
  "file:<filename>" (e.g. effect "Write the report file:report.md").
  Report content is rendered deterministically from completed RESEARCH
  evidence in the same graph; do not paste report text into the plan.
- RESEARCH nodes: put the research question in effect.
"""
_RISK_RE = re.compile(r"R[0-4]")
_MAX_NODES = 32


class SparkUnavailable(Exception):
    """Muse Spark cannot compile right now (missing key, timeout, provider down)."""


def _cap(text: Any, limit: int) -> str:
    return str(text or "")[:limit]


def _cap_json(payload: Any, limit: int) -> str:
    try:
        return json.dumps(payload, default=str)[:limit]
    except (TypeError, ValueError):
        return _cap(payload, limit)


def _bounded_context(contract: PresenceContract, context: dict[str, Any] | None) -> str:
    ctx = dict(context or {})
    capabilities = ctx.get("capabilities")
    if isinstance(capabilities, list):
        capabilities = [str(c)[:80] for c in capabilities[:24]]
    else:
        capabilities = []
    lines = [
        f"objective: {_cap(contract.objective, 500)}",
        f"criteria: {_cap_json(contract.success_criteria, 800)}",
        f"constraints: {_cap_json(contract.constraints, 800)}",
        f"autonomy: {_cap(contract.autonomy_policy, 24)}",
        f"risk_ceiling: {_cap(contract.risk_ceiling, 8)}",
        f"situation: {_cap_json(ctx.get('situation'), 800)}",
        f"capabilities: {_cap_json(capabilities, 800)}",
        f"devices: {_cap_json(contract.target_devices, 500)}",
        f"evidence: {_cap_json(contract.evidence, 800)}",
    ]
    return "\n".join(lines)


def _parse_graph(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"presence graph unparseable: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("presence graph unparseable: top level must be an object")
    return data


def _risk_level(risk: str) -> int:
    return int(risk[1:])


def _validate(data: dict[str, Any], *, risk_ceiling: str) -> tuple[list[dict[str, Any]], list[str]]:
    kinds = {k.value for k in NodeKind}
    targets = {t.value for t in NodeTarget}
    raw_nodes = data.get("nodes")
    if not isinstance(raw_nodes, list):
        raise ValueError("presence graph unparseable: 'nodes' must be a list")

    ids: set[str] = set()
    for entry in raw_nodes[:_MAX_NODES]:
        if isinstance(entry, dict):
            node_id = str(entry.get("node_id") or "").strip()
            if node_id:
                ids.add(node_id[:80])

    valid: list[dict[str, Any]] = []
    warnings: list[str] = []
    for entry in raw_nodes[:_MAX_NODES]:
        if not isinstance(entry, dict):
            warnings.append("dropped non-object node")
            continue
        node_id = str(entry.get("node_id") or "").strip()[:80]
        kind = str(entry.get("kind") or "").strip()
        target = str(entry.get("target") or "").strip()
        risk = str(entry.get("risk") or "R1").strip()
        depends = entry.get("depends_on") or []
        if not node_id:
            warnings.append("dropped node with missing node_id")
            continue
        if kind not in kinds:
            warnings.append(f"dropped node {node_id}: unknown kind {kind!r}")
            continue
        if target not in targets:
            warnings.append(f"dropped node {node_id}: unknown target {target!r}")
            continue
        if not _RISK_RE.fullmatch(risk):
            warnings.append(f"dropped node {node_id}: bad risk {risk!r}")
            continue
        if not isinstance(depends, list) or any(not isinstance(d, str) for d in depends):
            warnings.append(f"dropped node {node_id}: depends_on must be a string list")
            continue
        unknown = [d for d in depends if d not in ids]
        if unknown:
            warnings.append(f"dropped node {node_id}: unknown depends_on {unknown}")
            continue
        if _risk_level(risk) > _risk_level(risk_ceiling):
            warnings.append(f"node {node_id} risk {risk} above ceiling {risk_ceiling} (kept)")
        payload = entry.get("payload")
        if payload is not None and not isinstance(payload, dict):
            warnings.append(f"dropped node {node_id}: payload must be an object")
            continue
        payload = dict(payload or {})
        if kind == "VERIFY":
            text = str(entry.get("verification") or "")
            match = re.fullmatch(r"artifact_exists:([A-Za-z0-9][\w\-.]{0,119})", text.strip())
            if match:
                payload.setdefault("predicate", "artifact_exists")
                payload.setdefault("filename", match.group(1))
            else:
                warnings.append(f"node {node_id}: VERIFY without machine predicate will fail honestly")
        if kind == "ARTIFACT_OPERATION":
            match = re.search(r"file:([A-Za-z0-9][\w\-.]{0,119})", str(entry.get("effect") or ""))
            if match:
                payload.setdefault("filename", match.group(1))
        valid.append(
            {
                "node_id": node_id,
                "kind": kind,
                "target": target,
                "effect": str(entry.get("effect") or "")[:500],
                "risk": risk,
                "depends_on": [str(d)[:80] for d in depends],
                "verification": str(entry.get("verification") or "")[:500],
                "payload": {str(k)[:80]: v for k, v in list(payload.items())[:16]},
            }
        )
    return valid, warnings

async def compile_graph(
    session: AsyncSession,
    contract: PresenceContract,
    *,
    context: dict[str, Any] | None = None,
    budget_s: float = 20,
    persist: bool = True,
) -> dict[str, Any]:
    """Compile a contract into a validated work graph via Spark. Fail-closed."""
    from app.gateway.muse import (
        MuseProviderUnavailable,
        muse_spark_key_loaded,
        muse_spark_model,
    )
    from app.gateway.muse_spark import muse_spark_provider

    if not muse_spark_key_loaded():
        raise SparkUnavailable("OPENCODE_API_KEY missing: cannot compile")

    from app.contracts import ChatMessage
    from app.device_gateway.telemetry import emit
    from app.presence import service as presence_service

    goal_id = str(contract.id)
    model = muse_spark_model()
    messages = [
        ChatMessage(role="system", content=_SPARK_SYSTEM),
        ChatMessage(
            role="user",
            content="Compile this intent contract into a work graph:\n"
            + _bounded_context(contract, context),
        ),
    ]
    emit("presence.spark_call", goal_id=goal_id, model=model)
    try:
        result = await asyncio.wait_for(
            muse_spark_provider().chat_structured(
                messages,
                schema=GRAPH_SCHEMA,
                schema_name="presence_graph",
                model=model,
            ),
            timeout=budget_s,
        )
    except (MuseProviderUnavailable, TimeoutError) as exc:
        raise SparkUnavailable(f"spark unavailable: {exc}") from exc

    data = _parse_graph(getattr(result, "text", None) or "")
    ceiling = str(contract.risk_ceiling or "R2")[:8]
    if not _RISK_RE.fullmatch(ceiling):
        ceiling = "R2"
    valid, warnings = _validate(data, risk_ceiling=ceiling)

    nodes: list[dict[str, Any]] = []
    payloads = {n["node_id"]: n.get("payload") or {} for n in valid}
    if not persist:
        return {"nodes": valid, "warnings": warnings, "model": model}
    for node in valid:
        stored = await presence_service.upsert_node(
            session,
            contract,
            node_id=node["node_id"],
            kind=node["kind"],
            target=node["target"],
            effect=node["effect"],
            risk=node["risk"],
            depends_on=node["depends_on"],
            verification=node["verification"],
        )
        if payloads.get(node["node_id"]):
            graph = dict(contract.graph or {})
            items = [dict(n) for n in graph.get("nodes") or []]
            for item in items:
                if item.get("node_id") == node["node_id"]:
                    item["payload"] = payloads[node["node_id"]]
                    stored = item
            graph["nodes"] = items
            contract.graph = graph
            await session.flush()
        nodes.append(stored)
    logger.info("presence graph compiled goal=%s nodes=%d", goal_id, len(nodes))
    return {"nodes": nodes, "warnings": warnings, "model": model}
