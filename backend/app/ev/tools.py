"""Tool orchestration: registry + declarative dispatcher for EV's memory tools.

Every tool is a formally declared capability with an explicit input schema,
output shape, permission scope, read-only boundary, and undoability marker.
The dispatcher validates arguments and output shape, enforces the permission
matrix (sensitive tools need an explicit gate), and logs every invocation to
the access log so each call is authorized, auditable, and traceable.
"""

from __future__ import annotations

import ast
import math
import operator
import time
from collections.abc import Callable
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev import health_radar, maker, people
from app.ev.research import list_sessions
from app.gateway.validation import validate_arguments, validate_output
from app.memory.retrieval import Retriever
from app.models import GearSnapshot, Memory
from app.schemas import ToolCallResponse
from app.search.providers import get_search_provider
from app.services.access_log import log_access

MAX_EXPRESSION_LENGTH = 200
MAX_EXPONENT = 100
MAX_RESULT_ABS = 1e12


def safe_calculate(expression: str) -> float:
    """Evaluate simple arithmetic with a whitelisted AST (no eval/exec)."""
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise ValueError("Expression too long")
    allowed_nodes = (
        ast.Expression,
        ast.Constant,
        ast.BinOp,
        ast.UnaryOp,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.Pow,
        ast.Mod,
        ast.USub,
        ast.UAdd,
    )
    tree = ast.parse(expression, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, allowed_nodes):
            raise ValueError("Unsupported expression")

    ops: dict[type, Callable[..., float]] = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.Pow: operator.pow,
        ast.Mod: operator.mod,
        ast.USub: operator.neg,
        ast.UAdd: operator.pos,
    }

    def checked(value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("Result is not finite")
        if abs(value) > MAX_RESULT_ABS:
            raise ValueError("Result out of range")
        return float(value)

    def evaluate(node: ast.AST) -> float:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ValueError("Only numbers are allowed")
            return float(node.value)
        if isinstance(node, ast.BinOp):
            left = evaluate(node.left)
            right = evaluate(node.right)
            if isinstance(node.op, (ast.Div, ast.Mod)) and right == 0:
                raise ValueError("Division by zero")
            if isinstance(node.op, ast.Pow) and abs(right) > MAX_EXPONENT:
                raise ValueError("Exponent too large")
            op = cast(Callable[[float, float], float], ops[type(node.op)])
            return checked(float(op(left, right)))
        if isinstance(node, ast.UnaryOp):
            unary_op = cast(Callable[[float], float], ops[type(node.op)])
            return checked(float(unary_op(evaluate(node.operand))))
        raise ValueError("Unsupported expression")

    return checked(evaluate(tree.body))


TOOL_SPECS = [
    {
        "name": "search_memory",
        "description": "Search EV's personal memory (facts, decisions, goals, preferences).",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 1000},
                "k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
                "memory_type": {
                    "type": "string",
                    "enum": [
                        "decision",
                        "goal",
                        "preference",
                        "fact",
                        "observation",
                        "episodic",
                        "pattern",
                        "summary",
                        "lesson",
                    ],
                    "default": None,
                },
            },
            "required": ["query"],
        },
        "output": {"type": "object", "required": ["count", "results"]},
        "sensitive": False,
        "read_only": True,
        "permission": "memory:read",
        "undoable": False,
    },
    {
        "name": "search_decisions",
        "description": "Search past decisions (current and historical decision memory).",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 1000},
                "k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
            },
            "required": ["query"],
        },
        "output": {"type": "object", "required": ["count", "results"]},
        "sensitive": False,
        "read_only": True,
        "permission": "memory:read",
        "undoable": False,
    },
    {
        "name": "search_timeline",
        "description": "Search the raw event timeline.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 1000},
                "k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
            },
            "required": ["query"],
        },
        "output": {"type": "object", "required": ["count", "results"]},
        "sensitive": False,
        "read_only": True,
        "permission": "memory:read",
        "undoable": False,
    },
    {
        "name": "get_person",
        "description": "Find where a person appears in the user's memory.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 200}},
            "required": ["name"],
        },
        "output": {"type": "object", "required": ["name", "total_events"]},
        "sensitive": False,
        "read_only": True,
        "permission": "memory:read",
        "undoable": False,
    },
    {
        "name": "get_project",
        "description": "Get a maker project with BOM and print queue.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 200}},
            "required": ["name"],
        },
        "output": {"type": "object", "required": ["project", "bom", "print_jobs"]},
        "sensitive": False,
        "read_only": True,
        "permission": "memory:read",
        "undoable": False,
    },
    {
        "name": "get_goals",
        "description": "List current goals.",
        "parameters": {"type": "object", "additionalProperties": False, "properties": {}},
        "output": {"type": "object", "required": ["count", "goals"]},
        "sensitive": False,
        "read_only": True,
        "permission": "memory:read",
        "undoable": False,
    },
    {
        "name": "get_patterns",
        "description": "List detected behavior patterns, optionally by topic.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"topic": {"type": "string", "maxLength": 200, "default": None}},
        },
        "output": {"type": "object", "required": ["count", "patterns"]},
        "sensitive": False,
        "read_only": True,
        "permission": "memory:read",
        "undoable": False,
    },
    {
        "name": "calculate",
        "description": "Safe arithmetic calculation.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "expression": {"type": "string", "minLength": 1, "maxLength": MAX_EXPRESSION_LENGTH}
            },
            "required": ["expression"],
        },
        "output": {"type": "object", "required": ["expression", "result"]},
        "sensitive": False,
        "read_only": True,
        "permission": "compute:safe",
        "undoable": False,
    },
    {
        "name": "get_health_trends",
        "description": "Get health metric trends (sleep_hours, hrv_ms, resting_hr, steps, mood).",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "metric": {
                    "type": "string",
                    "enum": ["sleep_hours", "hrv_ms", "resting_hr", "steps", "mood"],
                },
                "window_days": {"type": "integer", "minimum": 1, "maximum": 90, "default": 14},
            },
            "required": ["metric"],
        },
        "output": {"type": "object", "required": ["metric"]},
        "sensitive": True,
        "read_only": True,
        "permission": "health:read",
        "undoable": False,
    },
    {
        "name": "get_gear_status",
        "description": "Latest gear telemetry for a device.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"device_id": {"type": "string", "maxLength": 200, "default": None}},
        },
        "output": {"type": "object", "required": ["found"]},
        "sensitive": False,
        "read_only": True,
        "permission": "gear:read",
        "undoable": False,
    },
    {
        "name": "get_upcoming_alerts",
        "description": "List pending EV alerts.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}},
        },
        "output": {"type": "object", "required": ["count", "alerts"]},
        "sensitive": False,
        "read_only": True,
        "permission": "alerts:read",
        "undoable": False,
    },
    {
        "name": "get_research",
        "description": "List research sessions, optionally open only.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "status": {"type": "string", "enum": ["open", "concluded"], "default": None},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
        },
        "output": {"type": "object", "required": ["count", "sessions"]},
        "sensitive": False,
        "read_only": True,
        "permission": "research:read",
        "undoable": False,
    },
    {
        "name": "search_web",
        "description": (
            "Search the web for current external information. Every result carries "
            "a citation (title, url, snippet); never present uncited web content as memory."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
            },
            "required": ["query"],
        },
        "output": {"type": "object", "required": ["count", "results"]},
        "sensitive": False,
        "read_only": True,
        "permission": "web:search",
        "undoable": False,
    },
]


def get_spec(name: str) -> dict | None:
    """Return the declared spec for a tool name, or None when unknown."""
    for spec in TOOL_SPECS:
        if spec["name"] == name:
            return spec
    return None


def list_tools() -> list[dict]:
    return list(TOOL_SPECS)


async def dispatch(
    session: AsyncSession,
    name: str,
    arguments: dict,
    *,
    actor: str = "master",
    allow_sensitive: bool = False,
    request_id: str | None = None,
) -> ToolCallResponse:
    """Validate, authorize, execute, shape-check, and log one tool invocation."""

    started = time.perf_counter()
    spec = get_spec(name)
    status = "ok"
    error: str | None = None
    result: dict | None = None

    if spec is None:
        status = "error"
        error = f"Unknown tool '{name}'"
    elif spec["sensitive"] and not allow_sensitive:
        status = "denied"
        error = f"Permission denied: '{name}' requires explicit permission before execution"
    else:
        effective, issues = validate_arguments(arguments, spec["parameters"])
        if issues:
            status = "rejected"
            error = "Invalid arguments: " + "; ".join(issues)
        else:
            try:
                result = await _handle(session, name, effective)
                output_issues = validate_output(result, spec.get("output") or {})
                if output_issues:
                    status = "error"
                    error = "Output validation failed: " + "; ".join(output_issues)
                    result = None
            except KeyError as exc:
                status = "error"
                error = str(exc)
            except Exception as exc:  # noqa: BLE001 - tool boundary
                status = "error"
                error = f"{type(exc).__name__}: {exc}"

    latency_ms = round((time.perf_counter() - started) * 1000, 1)
    response = ToolCallResponse(
        name=name,
        ok=status == "ok",
        result=result,
        error=error,
        latency_ms=latency_ms,
        request_id=request_id,
        actor=actor,
    )
    await log_access(
        session,
        actor=actor,
        action="tool_call",
        endpoint="POST /v1/gateway/tools",
        resource_type="tool",
        resource_ids=[name],
        request_id=request_id,
        details={
            "status": status,
            "latency_ms": latency_ms,
            "permission": spec["permission"] if spec else None,
            "sensitive": bool(spec and spec["sensitive"]),
            "read_only": bool(spec and spec["read_only"]),
            "error": error,
        },
    )
    return response


async def _handle(session: AsyncSession, name: str, args: dict) -> dict:
    retriever = Retriever(session)
    if name == "search_memory":
        memory_hits = await retriever.search(
            str(args.get("query", "")),
            k=int(args.get("k", 10)),
            access="model",
            memory_types=[args["memory_type"]] if args.get("memory_type") else None,
        )
        return {
            "count": len(memory_hits),
            "results": [
                {
                    "id": h.memory_id,
                    "text": h.text,
                    "memory_type": h.memory_type,
                    "score": h.score,
                    "date": h.event_time.isoformat() if h.event_time else None,
                    "provenance": h.source_event_ids,
                }
                for h in memory_hits[: int(args.get("k", 10))]
            ],
        }
    if name == "search_decisions":
        decision_hits = await retriever.search(
            str(args.get("query", "")),
            k=int(args.get("k", 10)),
            access="model",
            memory_types=["decision"],
        )
        return {
            "count": len(decision_hits),
            "results": [
                {
                    "id": h.memory_id,
                    "text": h.text,
                    "memory_type": h.memory_type,
                    "score": h.score,
                    "date": h.event_time.isoformat() if h.event_time else None,
                    "provenance": h.source_event_ids,
                }
                for h in decision_hits[: int(args.get("k", 10))]
            ],
        }
    if name == "search_timeline":
        timeline_hits = await retriever.search_events(
            str(args.get("query", "")), k=int(args.get("k", 10)), access="model"
        )
        return {"count": len(timeline_hits), "results": timeline_hits}
    if name == "get_person":
        info = await people.whereabouts(session, str(args["name"]))
        return info.model_dump()
    if name == "get_project":
        project = await maker.find_project_by_name(session, str(args["name"]))
        if project is None:
            raise KeyError(f"No project matching '{args['name']}'")
        return {
            "project": {
                "id": str(project.id),
                "name": project.name,
                "status": project.status,
                "current_step": project.current_step,
                "next_step": maker.next_step(project),
            },
            "bom": [
                {
                    "id": str(item.id),
                    "name": item.name,
                    "qty": item.qty,
                    "unit": item.unit,
                    "location": item.location,
                    "reorder_at": item.reorder_at,
                }
                for item in await maker.list_bom(session, project.id)
            ],
            "print_jobs": [
                {
                    "id": str(job.id),
                    "name": job.name,
                    "status": job.status,
                }
                for job in await maker.list_print_jobs(session, project.id)
            ],
        }
    if name == "get_goals":
        rows = (
            await session.execute(
                select(Memory).where(
                    Memory.memory_type == "goal",
                    Memory.is_current.is_(True),
                    Memory.redacted.is_(False),
                )
            )
        ).scalars().all()
        return {"count": len(rows), "goals": [{"id": str(m.id), "text": m.text} for m in rows]}
    if name == "get_patterns":
        stmt = (
            select(Memory)
            .where(
                Memory.memory_type == "pattern",
                Memory.is_current.is_(True),
                Memory.redacted.is_(False),
            )
            .order_by(Memory.confidence.desc())
        )
        rows = list((await session.execute(stmt)).scalars().all())
        topic = (args.get("topic") or "").lower()
        if topic:
            rows = [m for m in rows if topic in (m.payload or {}).get("topic", "").lower()]
        return {
            "count": len(rows),
            "patterns": [
                {
                    "id": str(m.id),
                    "text": m.text,
                    "kind": (m.payload or {}).get("kind"),
                    "confidence": m.confidence,
                    "evidence": (m.payload or {}).get("evidence", []),
                }
                for m in rows
            ],
        }
    if name == "calculate":
        return {"expression": args["expression"], "result": safe_calculate(str(args["expression"]))}
    if name == "get_health_trends":
        trend = await health_radar.trend(
            session,
            metric=str(args["metric"]),
            window_days=int(args.get("window_days", 14)),
        )
        return trend
    if name == "get_gear_status":
        gear_stmt = select(GearSnapshot).order_by(GearSnapshot.reported_at.desc()).limit(1)
        if args.get("device_id"):
            gear_stmt = gear_stmt.where(GearSnapshot.device_id == str(args["device_id"]))
        row = (await session.execute(gear_stmt)).scalars().first()
        if row is None:
            return {"found": False}
        return {
            "found": True,
            "device_id": row.device_id,
            "reported_at": row.reported_at.isoformat(),
            "battery_percent": row.battery_percent,
            "storage_free_bytes": row.storage_free_bytes,
            "cpu_percent": row.cpu_percent,
            "memory_used_percent": row.memory_used_percent,
        }
    if name == "get_upcoming_alerts":
        from app.ev import alert_radar

        alerts = await alert_radar.list_alerts(session, status="pending", limit=int(args.get("limit", 10)))
        return {
            "count": len(alerts),
            "alerts": [
                {
                    "id": str(a.id),
                    "kind": a.kind,
                    "title": a.title,
                    "body": a.body,
                    "priority": a.priority,
                    "tier": a.tier,
                }
                for a in alerts
            ],
        }
    if name == "get_research":
        sessions = await list_sessions(session, status=args.get("status"), limit=int(args.get("limit", 10)))
        return {
            "count": len(sessions),
            "sessions": [
                {
                    "id": str(s.id),
                    "question": s.question,
                    "status": s.status,
                    "conclusion": s.conclusion,
                }
                for s in sessions
            ],
        }
    if name == "search_web":
        provider = get_search_provider()
        if provider is None:
            raise KeyError(
                "Web search is disabled: set EV_SEARCH_PROVIDER and an API key to enable it"
            )
        results = await provider.search(
            str(args["query"]),
            limit=int(args.get("limit", 5)),
        )
        return {
            "count": len(results),
            "results": [
                {"title": r.title, "url": r.url, "snippet": r.snippet}
                for r in results
            ],
        }
    raise KeyError(f"Unknown tool '{name}'")
