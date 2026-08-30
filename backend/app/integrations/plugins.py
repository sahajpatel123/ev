"""Plugin framework: manifests, approval, and sandboxed command execution.

Plugins extend EV with custom skills/commands without touching core code. A
plugin is inert until its manifest is validated and explicitly approved by the
master key; commands run in a subprocess with an isolated interpreter,
AST-level sandbox rules, no network/filesystem/import access, and only the
capabilities declared in the approved manifest (least privilege).
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.ev.live import create_channel, ingest_events, query_live_events
from app.models import LiveChannel, Memory, Plugin
from app.schemas import (
    LiveChannelCreate,
    LiveEventCreate,
    LiveEventOut,
    PluginCommandOut,
    PluginManifest,
    PluginOut,
)
from app.services.access_log import log_access
from app.utils.text import canonical_json, sha256_hex, utcnow

PLUGIN_CAPABILITIES = frozenset({"memory:read", "live:read", "live:emit"})

SAFE_BUILTINS = frozenset(
    {
        "len",
        "sum",
        "min",
        "max",
        "abs",
        "round",
        "sorted",
        "reversed",
        "zip",
        "enumerate",
        "range",
        "str",
        "int",
        "float",
        "bool",
        "list",
        "dict",
        "set",
        "tuple",
        "isinstance",
        "issubclass",
        "format",
        "repr",
        "any",
        "all",
        "filter",
        "map",
        "chr",
        "ord",
        "hex",
        "oct",
        "bin",
        "divmod",
        "pow",
        "next",
        "iter",
    }
)

DANGEROUS_CALLS = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "open",
        "input",
        "globals",
        "locals",
        "vars",
        "dir",
        "getattr",
        "setattr",
        "delattr",
        "hasattr",
        "type",
        "object",
        "super",
        "breakpoint",
        "memoryview",
        "__import__",
        "exit",
        "quit",
        "help",
        "copyright",
        "license",
        "print",
    }
)

MAX_HANDLER_LENGTH = 20_000
MAX_EMIT_EVENTS = 50


def _plugin_out(row: Plugin) -> PluginOut:
    return PluginOut(
        id=row.id,
        slug=row.slug,
        name=row.name,
        version=row.version,
        status=row.status,
        permissions=row.permissions or [],
        checksum=row.checksum,
        manifest=row.manifest or {},
        approved_at=row.approved_at,
        approved_by=row.approved_by,
        rejected_reason=row.rejected_reason,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _validate_handler_source(source: str) -> None:
    if len(source) > MAX_HANDLER_LENGTH:
        raise ValueError("plugin handler exceeds the size limit")
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        raise ValueError(f"plugin handler has invalid syntax: {exc.msg}") from None
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal)):
            raise ValueError(f"plugin handler cannot use {type(node).__name__}")
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise ValueError(f"plugin handler cannot reference dunder name '{node.id}'")
        if isinstance(node, ast.Attribute) and (
            node.attr.startswith("__") or node.attr in {"__class__", "__globals__", "__builtins__"}
        ):
            raise ValueError(f"plugin handler cannot access attribute '{node.attr}'")
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in DANGEROUS_CALLS:
                raise ValueError(f"plugin handler cannot call '{func.id}'")
            if isinstance(func, ast.Attribute) and (
                func.attr.startswith("__") or func.attr in DANGEROUS_CALLS
            ):
                raise ValueError(f"plugin handler cannot call '{func.attr}'")


def validate_manifest(manifest: dict) -> tuple[PluginManifest, list[str]]:
    """Validate a plugin manifest; returns (parsed, errors)."""
    try:
        parsed = PluginManifest.model_validate(manifest)
    except ValidationError as exc:
        parse_errors = []
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"])
            parse_errors.append(f"{location}: {error['msg']}")
        return (
            PluginManifest(name="invalid", slug="invalid", version="0", commands=[{}]),
            parse_errors,
        )
    validation_errors: list[str] = []
    permissions = set(parsed.permissions)
    unknown = permissions - PLUGIN_CAPABILITIES
    if unknown:
        validation_errors.append(
            f"permissions must be a subset of {sorted(PLUGIN_CAPABILITIES)}; unknown: {sorted(unknown)}"
        )
    if not parsed.commands:
        validation_errors.append("a plugin must declare at least one command")
    for index, command in enumerate(parsed.commands):
        name = command.get("name")
        if not isinstance(name, str) or not name or len(name) > 64:
            validation_errors.append(
                f"commands[{index}].name must be a non-empty string <= 64 chars"
            )
        permission = command.get("permission")
        if permission not in permissions:
            validation_errors.append(
                f"commands[{index}].permission '{permission}' is not declared in permissions"
            )
        handler = command.get("handler")
        if not isinstance(handler, str) or not handler.strip():
            validation_errors.append(
                f"commands[{index}].handler must be the non-empty body of 'def run(args, context)'"
            )
        else:
            try:
                _validate_handler_source(handler)
            except ValueError as exc:
                validation_errors.append(f"commands[{index}].handler: {exc}")
    return parsed, validation_errors


def checksum(manifest: dict) -> str:
    return sha256_hex(canonical_json(manifest))


async def submit(session: AsyncSession, manifest: dict, actor: str) -> Plugin:
    parsed, errors = validate_manifest(manifest)
    if errors:
        raise ValueError("; ".join(errors))
    manifest_dict = parsed.model_dump()
    digest = checksum(manifest_dict)
    existing = (
        await session.execute(select(Plugin).where(Plugin.slug == parsed.slug))
    ).scalar_one_or_none()
    if existing is not None:
        if existing.checksum == digest:
            return existing
        raise ValueError(f"plugin slug '{parsed.slug}' already exists with different content")
    row = Plugin(
        slug=parsed.slug,
        name=parsed.name,
        version=parsed.version,
        status="pending",
        manifest=manifest_dict,
        permissions=sorted(set(parsed.permissions)),
        checksum=digest,
    )
    session.add(row)
    await session.flush()
    await log_access(
        session,
        actor=actor,
        action="plugin.submit",
        endpoint="POST /v1/plugins",
        resource_type="plugin",
        resource_ids=[row.id],
        details={"slug": row.slug, "permissions": row.permissions},
    )
    return row


async def _master_only(actor: str) -> None:
    if actor != "master":
        raise PermissionError("only the master key can manage plugin lifecycle")


async def approve(
    session: AsyncSession,
    plugin_id: UUID,
    actor: str,
    reason: str = "approved by owner",
) -> PluginOut:
    await _master_only(actor)
    row = await session.get(Plugin, plugin_id)
    if row is None:
        raise KeyError(plugin_id)
    row.status = "approved"
    row.approved_at = utcnow()
    row.approved_by = actor
    row.rejected_reason = None
    await log_access(
        session,
        actor=actor,
        action="plugin.approve",
        endpoint="POST /v1/plugins/{id}/approve",
        resource_type="plugin",
        resource_ids=[row.id],
        details={"slug": row.slug, "reason": reason},
    )
    return _plugin_out(row)


async def reject(
    session: AsyncSession,
    plugin_id: UUID,
    actor: str,
    reason: str,
) -> PluginOut:
    await _master_only(actor)
    row = await session.get(Plugin, plugin_id)
    if row is None:
        raise KeyError(plugin_id)
    row.status = "rejected"
    row.rejected_reason = reason
    await log_access(
        session,
        actor=actor,
        action="plugin.reject",
        resource_type="plugin",
        resource_ids=[row.id],
        details={"slug": row.slug, "reason": reason},
    )
    return _plugin_out(row)


async def set_enabled(
    session: AsyncSession,
    plugin_id: UUID,
    actor: str,
    enabled: bool,
) -> PluginOut:
    await _master_only(actor)
    row = await session.get(Plugin, plugin_id)
    if row is None:
        raise KeyError(plugin_id)
    if enabled and row.status != "disabled":
        raise PermissionError("only a disabled plugin can be enabled")
    if not enabled and row.status != "approved":
        raise PermissionError("only an approved plugin can be disabled")
    row.status = "approved" if enabled else "disabled"
    await log_access(
        session,
        actor=actor,
        action="plugin.enable" if enabled else "plugin.disable",
        resource_type="plugin",
        resource_ids=[row.id],
        details={"slug": row.slug},
    )
    return _plugin_out(row)


async def list_plugins(session: AsyncSession) -> list[PluginOut]:
    rows = (
        await session.execute(select(Plugin).order_by(Plugin.created_at.desc()))
    ).scalars().all()
    return [_plugin_out(row) for row in rows]


async def get_plugin(session: AsyncSession, plugin_id: UUID) -> PluginOut:
    row = await session.get(Plugin, plugin_id)
    if row is None:
        raise KeyError(plugin_id)
    return _plugin_out(row)


def _wrap_handler(handler: str) -> str:
    indented = "\n".join(("    " + line if line.strip() else line) for line in handler.splitlines())
    return (
        "import json,sys\n"
        "def run(args, context):\n"
        f"{indented}\n"
        "def main():\n"
        "    data=json.load(sys.stdin)\n"
        "    out=run(data.get('args',{}), data.get('context',{}))\n"
        "    sys.stdout.write(json.dumps({'result': out}))\n"
        "main()\n"
    )


async def _plugin_context(session: AsyncSession, permissions: list[str]) -> dict:
    context: dict = {}
    if "memory:read" in permissions:
        memory_rows = (
            await session.execute(
                select(Memory)
                .where(
                    Memory.is_current.is_(True),
                    Memory.privacy_level != "never_send_to_model",
                )
                .order_by(Memory.importance.desc())
                .limit(8)
            )
        ).scalars().all()
        context["memories"] = [
            {
                "text": memory.text,
                "memory_type": memory.memory_type,
                "importance": memory.importance,
            }
            for memory in memory_rows
        ]
    if "live:read" in permissions:
        live_rows = await query_live_events(session, access="model", limit=10)
        context["live_events"] = [
            {
                "event_type": live_event.event_type,
                "payload": live_event.payload,
                "occurred_at": live_event.occurred_at.isoformat(),
            }
            for live_event in live_rows
        ]
    return context


async def _plugin_channel(session: AsyncSession, slug: str) -> LiveChannel:
    row = (
        await session.execute(
            select(LiveChannel).where(
                LiveChannel.name == f"plugin:{slug}",
                LiveChannel.active.is_(True),
            )
        )
    ).scalars().first()
    if row is not None:
        return row
    return await create_channel(
        session,
        LiveChannelCreate(
            name=f"plugin:{slug}",
            kind="app",
            privacy_level="normal",
            metadata={"collector": f"plugin:{slug}"},
        ),
    )


async def run_command(
    session: AsyncSession,
    plugin_id: UUID,
    command_name: str,
    args: dict,
    actor: str,
    device_id=None,
) -> PluginCommandOut:
    row = await session.get(Plugin, plugin_id)
    if row is None:
        raise KeyError(plugin_id)
    if row.status != "approved":
        raise PermissionError("plugin is not approved")
    commands = {command.get("name"): command for command in row.manifest.get("commands", [])}
    spec = commands.get(command_name)
    if spec is None:
        raise KeyError(f"unknown command '{command_name}'")
    permission = spec.get("permission")
    if permission not in (row.permissions or []):
        raise PermissionError("command permission is not approved")
    from app.ev.policy import authorize

    policy_name = f"plugin:{row.slug}:{command_name}"
    policy_spec = {
        "name": policy_name,
        "description": f"Run the approved {row.slug} plugin command",
        "parameters": {"type": "object", "additionalProperties": True},
        "output": {"type": "object"},
        "permission": permission,
        "required_scopes": [permission],
        "read_only": permission.endswith(":read"),
        "sensitive": permission == "live:emit",
        "risk_class": "R1" if permission == "live:emit" else "R0",
        "confirmation": "none",
        "target_ownership": "owner",
        "provider": "local",
        "evidence": ["source", "timestamp"],
    }
    decision = await authorize(
        session,
        policy_name,
        actor=actor,
        device_id=device_id,
        channel="action",
        arguments=args or {},
        spec=policy_spec,
        provider_connected_override=True,
    )
    if not decision.allowed:
        raise PermissionError(decision.reason)
    handler = spec.get("handler")
    if not isinstance(handler, str):
        raise ValueError("plugin command handler is missing")
    _validate_handler_source(handler)
    context = await _plugin_context(session, row.permissions or [])
    wrapper = _wrap_handler(handler)
    try:
        # Process-level isolation only: `python -I -S` plus AST validation
        # rejects imports/dunders, but the subprocess still runs as the EV
        # server user with host network access. Plugins are master-approved
        # before any run; before plugin execution is exposed to untrusted or
        # external code, containerize it (see docs/SECURITY.md §13).
        proc = subprocess.run(
            [sys.executable, "-I", "-S", "-c", wrapper],
            input=json.dumps({"args": args or {}, "context": context}).encode("utf-8"),
            capture_output=True,
            timeout=settings.plugin_timeout_seconds,
            env={},
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("plugin command timed out") from exc
    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode("utf-8", "replace")[:500]
        raise ValueError(f"plugin command failed: {stderr or f'exit {proc.returncode}'}")
    stdout = (proc.stdout or b"")[: settings.plugin_max_output_bytes]
    try:
        output = json.loads(stdout.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("plugin command produced invalid output") from exc
    result = output.get("result") if isinstance(output, dict) else None
    if not isinstance(result, dict):
        raise ValueError("plugin command must return a JSON object")

    emitted: list[LiveEventOut] = []
    emit = result.get("emit")
    if emit and "live:emit" in (row.permissions or []):
        if not isinstance(emit, list) or not emit or len(emit) > MAX_EMIT_EVENTS:
            raise ValueError(
                f"plugin 'emit' must be a list of 1..{MAX_EMIT_EVENTS} event objects"
            )
        channel = await _plugin_channel(session, row.slug)
        events = []
        for event in emit:
            if not isinstance(event, dict):
                raise ValueError("plugin emitted events must be objects")
            events.append(
                LiveEventCreate(
                    event_type=str(event.get("event_type") or "plugin.event")[:64],
                    payload=dict(event.get("payload") or {}),
                )
            )
        stored = await ingest_events(session, channel, events)
        emitted = [LiveEventOut.model_validate(event) for event in stored]
    await log_access(
        session,
        actor=actor,
        action="plugin.run",
        endpoint="POST /v1/plugins/{id}/commands/{command}",
        resource_type="plugin",
        resource_ids=[row.id],
        details={
            "plugin": row.slug,
            "command": command_name,
            "emitted": len(emitted),
            "policy_effect": decision.effect,
            "risk_class": decision.risk_class,
        },
    )
    return PluginCommandOut(
        plugin_id=row.id,
        plugin=row.slug,
        command=command_name,
        result=result,
        emitted_events=emitted,
        executed_at=utcnow(),
    )
