"""Authoritative runtime projection for executable EV capabilities.

Capability declarations continue to live in the existing tool/action/fleet
specifications.  This module is the one runtime *projection* of those
declarations: it joins them with provider, device, credential, and safety
state before a client or model is allowed to see them.  It is deliberately
not a second registry.

The projection is also the boundary for Realtime tool construction.  A model
tool is emitted only when the capability is currently available, non-forbidden,
and eligible for model exposure.  Dispatch still performs the authoritative
policy check; availability here is an exposure decision, not authorization.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Device, Integration, IntegrationCredential, OwnerCamera
from app.utils.text import utcnow

logger = logging.getLogger("ev.capabilities")

MANIFEST_SCHEMA_VERSION = "ev.capability-manifest.v1"

# A small number of existing declarations predate provider metadata.  These
# are stable provider classifications, not new capabilities.  Keeping the
# overrides here makes the runtime projection honest until the source specs
# can carry the same metadata without changing their dispatch behavior.
PROVIDER_OVERRIDES: dict[str, str] = {
    "camera_replay": "camera",
    "look": "vision",
    "observe_camera": "vision",
    "computer_status": "computer",
    "list_apps": "computer",
    "activate_app": "computer",
    "inspect_ui": "computer",
    "ui_action": "computer",
    "screen_look": "computer",
    "app_action": "computer",
    "drone": "drone",
    "estimate_print": "printer",
    "print_start": "printer",
    "search_web": "search",
    "web_search": "search",
    "ticket_hold": "tickets",
    "ticket_buy": "tickets",
    "execute_command": "software",
}

INTEGRATION_PROVIDERS = frozenset(
    {"calendar", "messaging", "phone", "mail", "contacts", "drone", "tickets"}
)
LOCAL_PROVIDERS = frozenset({"local", "open-meteo", "software", "vision", "computer"})

RUNTIME_FIELDS = (
    "name",
    "version",
    "description",
    "parameters",
    "json_schema",
    "output",
    "provider",
    "current_provider",
    "device",
    "current_device",
    "availability",
    "availability_reason",
    "required_scopes",
    "risk_class",
    "confirmation",
    "confirmation_requirement",
    "evidence",
    "evidence_requirements",
    "cancellation",
    "fallback",
    "fallback_reason",
    "actor",
    "actor_kind",
    "device_id",
    "device_status",
    "device_authorized",
    "authorization",
    "authorization_effect",
    "authorization_reason",
    "approved",
    "approval",
    "approval_state",
    "approval_required",
    "executable",
    "executable_reason",
    "confirmation_required",
    "confirmation_satisfied",
    "confirmation_state",
    "confirmation_policy",
    "confirmation_ttl_seconds",
    "independent_confirmation",
    "model_exposed",
    "realtime_eligible",
)


def _scope_match(required: str, granted: Sequence[str]) -> bool:
    """Use the policy scope aliases without duplicating their definitions."""

    from app.ev.policy import SCOPE_ALIASES

    granted_set = {str(item) for item in granted}
    aliases = SCOPE_ALIASES.get(required, frozenset({required}))
    return bool(granted_set & aliases) or required in granted_set


def _declared_specs() -> list[dict[str, Any]]:
    """Read the existing declarations and return each name once."""

    from app.ev.actions import ACTION_SPECS
    from app.ev.tools import get_spec, list_tools

    names: list[str] = []
    for declared in [*list_tools(), *ACTION_SPECS]:
        name = str(declared.get("name") or "")
        if name and name not in names:
            names.append(name)

    specs: list[dict[str, Any]] = []
    for name in names:
        spec = get_spec(name)
        if spec is None:
            continue
        normalized = dict(spec)
        provider = PROVIDER_OVERRIDES.get(name)
        if provider:
            normalized["provider"] = provider
        specs.append(normalized)
    return specs


def _device_payload(device: Device, *, now: datetime) -> dict[str, Any]:
    last_seen = device.last_seen_at
    if last_seen is not None and last_seen.tzinfo is None:
        # SQLite returns DateTime(timezone=True) values without tzinfo. Keep
        # heartbeat freshness comparisons valid across SQLite and Postgres.
        last_seen = last_seen.replace(tzinfo=now.tzinfo)
    online = bool(
        last_seen is not None
        and now - last_seen <= timedelta(seconds=settings.runtime_heartbeat_grace_seconds)
    )
    return {
        "id": str(device.id),
        "name": device.name,
        "device_type": device.device_type,
        "platform": device.platform,
        "capabilities": list(device.capabilities or []),
        "trust_level": device.trust_level,
        "online": online,
        "last_seen_at": last_seen.isoformat() if last_seen else None,
    }


def _required_scopes(spec: Mapping[str, Any]) -> list[str]:
    from app.ev.policy import required_scopes_for

    return list(required_scopes_for(dict(spec)))


def _fallback(provider: str, spec: Mapping[str, Any]) -> str:
    declared = str(spec.get("fallback") or "").strip()
    name = str(spec.get("name") or "")
    if declared and PROVIDER_OVERRIDES.get(name) != provider:
        return declared
    if provider in INTEGRATION_PROVIDERS:
        return "not_connected: connect the provider and grant the required scope"
    if provider == "smart_home":
        return "not_connected: configure the local or Home Assistant bridge"
    if provider == "camera":
        return "not_connected: add an owner camera"
    if provider == "printer":
        return "not_connected: configure the printer bridge"
    if provider in {"search", "tickets"}:
        return "not_connected: configure the approved provider"
    if provider == "macos_life":
        return "not_connected: connect the macOS life helper"
    if provider == "computer":
        return "not_connected: connect EV.app for Mac control"
    if provider == "drone":
        return "not_connected: pair an owner drone"
    return "report unavailable; do not fabricate success"


def _projection_arguments(
    schema: Mapping[str, Any],
    *,
    tool_name: str | None = None,
) -> dict[str, Any]:
    """Create validation-only values for a capability-level policy check.

    The projection does not authorize a concrete invocation, so it must not
    invent user arguments.  It does, however, need a structurally valid shape
    to ask ``evaluate_policy`` whether this actor may use the capability once
    real arguments and any required confirmation arrive.
    """

    properties = schema.get("properties")
    properties = properties if isinstance(properties, Mapping) else {}
    result: dict[str, Any] = {}
    for key in schema.get("required") or []:
        name = str(key)
        declaration = properties.get(name)
        declaration = declaration if isinstance(declaration, Mapping) else {}
        if "default" in declaration and declaration.get("default") is not None:
            result[name] = declaration["default"]
            continue
        enum = declaration.get("enum")
        if isinstance(enum, list) and enum:
            result[name] = enum[0]
            continue
        kind = declaration.get("type")
        if kind in {"integer", "number"}:
            result[name] = declaration.get("minimum", 1)
        elif kind == "boolean":
            result[name] = False
        elif kind == "array":
            result[name] = []
        elif kind == "object":
            result[name] = {}
        else:
            result[name] = "projection-target"
    # A few safe, target-bound tools intentionally have no required schema
    # field. Supply validation-only values so policy can report confirmation
    # eligibility without treating the projection as an invocation.
    if tool_name == "place_call":
        result.setdefault("name", "projection-target")
    elif tool_name == "get_weather":
        result.setdefault("place", "home")
    elif tool_name == "home_status":
        result.setdefault("area", "home")
    elif tool_name == "open_url":
        result.setdefault("url", "https://example.com")
    elif tool_name in {"open_app", "close_app", "activate_app"}:
        result.setdefault("name", "Safari")
    elif tool_name == "ui_action":
        result.setdefault("action", "press")
    elif tool_name == "app_action":
        result.setdefault("action", "status")
    return result


def _actor_kind(actor: str) -> str:
    normalized = str(actor or "").strip().lower()
    if normalized.startswith("device:"):
        return "device"
    if normalized in {"master", "owner", "voice"}:
        return "owner"
    if normalized in {"worker", "scheduler", "job"}:
        return "worker"
    if normalized in {"model", "llm", "assistant"}:
        return "model"
    return "delegate"


def _integration_state(
    provider: str,
    *,
    required_scopes: Sequence[str],
    provider_rows: Mapping[str, Integration],
    credentialed_integrations: set[UUID],
) -> dict[str, Any]:
    row = provider_rows.get(provider)
    granted = list(row.scopes or []) if row is not None else []
    missing = [scope for scope in required_scopes if not _scope_match(scope, granted)]
    config = row.config if row is not None and isinstance(row.config, dict) else {}
    configured_provider = str(config.get("provider") or "").strip() or None
    credential_required = provider in {
        "calendar",
        "messaging",
        "phone",
        "mail",
        "contacts",
    } and configured_provider not in {"local", "macos_life", "device_proxy"}
    credential_ready = row is not None and (
        not credential_required or row.id in credentialed_integrations
    )
    if row is None:
        return {
            "availability": "not_connected",
            "reason": "no active provider connection",
            "current_provider": provider,
            "provider_scopes": [],
            "missing_provider_scopes": list(required_scopes),
            "credential_ready": False,
        }
    if missing:
        reason = "missing provider scope: " + ", ".join(missing)
        availability = "not_connected"
    elif not credential_ready:
        reason = "provider credential is not configured"
        availability = "not_connected"
    else:
        reason = "active provider connection"
        availability = "available"
    return {
        "availability": availability,
        "reason": reason,
        "current_provider": configured_provider or provider,
        "provider_scopes": granted,
        "missing_provider_scopes": missing,
        "credential_ready": credential_ready,
    }


def _provider_state(
    provider: str,
    *,
    required_scopes: Sequence[str],
    provider_rows: Mapping[str, Integration],
    credentialed_integrations: set[UUID],
    camera_available: bool,
) -> dict[str, Any]:
    """Resolve current availability for one declared provider."""

    if provider in {"local", "open-meteo", "software", "vision", "computer"}:
        return {
            "availability": "available",
            "reason": "local provider available",
            "current_provider": provider,
            "provider_scopes": [],
            "missing_provider_scopes": [],
            "credential_ready": True,
        }
    if provider == "camera":
        return {
            "availability": "available" if camera_available else "not_connected",
            "reason": "owner camera registered" if camera_available else "no owner camera registered",
            "current_provider": "owner_camera",
            "provider_scopes": [],
            "missing_provider_scopes": [] if camera_available else list(required_scopes),
            "credential_ready": camera_available,
        }
    if provider == "printer":
        if settings.octoprint_url:
            return {
                "availability": "available",
                "reason": "OctoPrint endpoint configured",
                "current_provider": "octoprint",
                "provider_scopes": [],
                "missing_provider_scopes": [],
                "credential_ready": True,
            }
        state = _integration_state(
            "printer",
            required_scopes=required_scopes,
            provider_rows=provider_rows,
            credentialed_integrations=credentialed_integrations,
        )
        return state
    if provider == "search":
        configured = str(settings.search_provider or "none").strip().lower()
        credential_ready = configured != "brave" or bool(
            str(settings.brave_search_api_key or "").strip()
        )
        if configured in {"mock", "live"} or (configured == "brave" and credential_ready):
            return {
                "availability": "available",
                "reason": f"search provider configured: {configured}",
                "current_provider": configured,
                "provider_scopes": [],
                "missing_provider_scopes": [],
                "credential_ready": credential_ready,
            }
        if configured == "brave":
            return {
                "availability": "not_connected",
                "reason": "Brave Search API key is not configured",
                "current_provider": configured,
                "provider_scopes": [],
                "missing_provider_scopes": ["EV_BRAVE_SEARCH_API_KEY"],
                "credential_ready": False,
            }
        return {
            "availability": "not_connected",
            "reason": "search provider is disabled",
            "current_provider": configured or "none",
            "provider_scopes": [],
            "missing_provider_scopes": list(required_scopes),
            "credential_ready": False,
        }
    if provider == "smart_home":
        row = provider_rows.get("smart_home")
        if row is None:
            return {
                "availability": "available",
                "reason": "local owner home double available",
                "current_provider": "local",
                "provider_scopes": [],
                "missing_provider_scopes": [],
                "credential_ready": True,
            }
        state = _integration_state(
            "smart_home",
            required_scopes=required_scopes,
            provider_rows=provider_rows,
            credentialed_integrations=credentialed_integrations,
        )
        config = row.config if isinstance(row.config, dict) else {}
        if str(config.get("provider") or "").strip().lower() in {"", "local"}:
            state.update(
                availability="available",
                reason="configured local home provider",
                current_provider="local",
                credential_ready=True,
            )
        elif not config.get("base_url") and state["availability"] == "available":
            state.update(
                availability="not_connected",
                reason="Home Assistant base URL is not configured",
                credential_ready=False,
            )
        return state
    if provider == "macos_life":
        from app.ev.apps import macos_life_from_rows

        row = macos_life_from_rows(dict(provider_rows))
        if row is None:
            return {
                "availability": "not_connected",
                "reason": "no macos_life helper bridge connected",
                "current_provider": "macos_life",
                "provider_scopes": [],
                "missing_provider_scopes": list(required_scopes),
                "credential_ready": False,
            }
        # The helper is the app/url actuator. Messaging/mail/phone scopes on
        # the same row do not have to name apps:act for open/close to be real.
        granted = list(row.scopes or [])
        for scope in required_scopes:
            if scope not in granted:
                granted.append(scope)
        return {
            "availability": "available",
            "reason": "macos_life helper bridge connected",
            "current_provider": "macos_life",
            "provider_scopes": granted,
            "missing_provider_scopes": [],
            "credential_ready": True,
        }
    if provider in INTEGRATION_PROVIDERS:
        return _integration_state(
            provider,
            required_scopes=required_scopes,
            provider_rows=provider_rows,
            credentialed_integrations=credentialed_integrations,
        )
    return {
        "availability": "unavailable",
        "reason": "provider adapter is not configured",
        "current_provider": provider,
        "provider_scopes": [],
        "missing_provider_scopes": list(required_scopes),
        "credential_ready": False,
    }


def _entry(
    spec: Mapping[str, Any],
    *,
    selected_device: dict[str, Any] | None,
    available_devices: list[dict[str, Any]],
    provider_rows: Mapping[str, Integration],
    credentialed_integrations: set[UUID],
    camera_available: bool,
) -> dict[str, Any]:
    from app.ev.policy import FORBIDDEN_NAMES, annotate_spec

    normalized = annotate_spec(dict(spec))
    name = str(normalized.get("name") or "")
    provider = str(normalized.get("provider") or "local")
    required_scopes = _required_scopes(normalized)
    risk_class = str(normalized.get("risk_class") or "R2")
    state = _provider_state(
        provider,
        required_scopes=required_scopes,
        provider_rows=provider_rows,
        credentialed_integrations=credentialed_integrations,
        camera_available=camera_available,
    )
    forbidden = name in FORBIDDEN_NAMES or risk_class == "forbidden"
    if forbidden:
        state = {
            **state,
            "availability": "unavailable",
            "reason": "capability is forbidden and is never model-exposed",
        }
    parameters = normalized.get("parameters") or normalized.get("payload") or {}
    realtime_eligible = (
        not forbidden
        and risk_class not in {"R4"}
        and state["availability"] == "available"
    )
    return {
        "name": name,
        "version": str(normalized.get("version") or "1"),
        "description": str(normalized.get("description") or ""),
        "parameters": parameters,
        "json_schema": parameters,
        "output": normalized.get("output") or {},
        "provider": provider,
        "current_provider": state["current_provider"],
        "device": selected_device,
        "current_device": selected_device,
        "available_devices": available_devices,
        "availability": state["availability"],
        "availability_reason": state["reason"],
        "required_scopes": required_scopes,
        "required_permission": normalized.get("required_permission")
        or (required_scopes[0] if required_scopes else None),
        "provider_scopes": state["provider_scopes"],
        "missing_provider_scopes": state["missing_provider_scopes"],
        "provider_credential_ready": state["credential_ready"],
        "risk_class": risk_class,
        "confirmation": normalized.get("confirmation"),
        "confirmation_requirement": normalized.get("confirmation"),
        "target_ownership": normalized.get("target_ownership"),
        "evidence": list(normalized.get("evidence") or []),
        "evidence_requirements": list(normalized.get("evidence") or []),
        "cancellation": normalized.get("cancellation"),
        "fallback": _fallback(provider, normalized),
        "idempotency": normalized.get("idempotency"),
        "timeout_seconds": normalized.get("timeout_seconds"),
        "audit_event": normalized.get("audit_event"),
        "model_exposed": realtime_eligible,
        "realtime_eligible": realtime_eligible,
    }


async def _authorization_state(
    session: AsyncSession,
    entry: dict[str, Any],
    spec: Mapping[str, Any],
    *,
    actor: str,
    device_id: UUID | None,
    channel: str | None,
    now: datetime,
    device_status_override: str | None = None,
) -> dict[str, Any]:
    """Project the policy decision for a capability-shaped invocation."""

    from app.ev.policy import INTEGRATION_PROVIDERS, authorize

    availability = str(entry.get("availability") or "unavailable")
    provider = str(entry.get("provider") or "local")
    provider_connected: bool | None
    if availability == "available":
        provider_connected = True
    elif availability == "not_connected":
        provider_connected = False
    else:
        provider_connected = None
    provider_scopes = (
        list(entry.get("provider_scopes") or [])
        if provider in INTEGRATION_PROVIDERS
        else None
    )
    decision = await authorize(
        session,
        str(entry["name"]),
        actor=actor,
        arguments=_projection_arguments(
            entry.get("json_schema") or {},
            tool_name=str(entry.get("name") or ""),
        ),
        device_id=device_id,
        channel=channel,
        spec=dict(spec),
        provider_scopes_override=provider_scopes,
        provider_connected_override=provider_connected,
        now=now,
    )
    device_status = decision.audit.get("device_status") or device_status_override
    invalid_device = device_status in {"revoked", "unknown", "untrusted"}
    effective_effect = "deny" if invalid_device else decision.effect
    effective_reason = (
        f"invalid actor/device combination: {device_status} device"
        if invalid_device
        else decision.reason
    )
    confirmation_required = bool(
        not invalid_device
        and (decision.confirmation_required or decision.effect == "confirm")
    )
    allowed = bool(decision.allowed and not invalid_device)
    confirmation_satisfied = bool(allowed and not confirmation_required)
    if allowed:
        approval_state = "approved"
    elif effective_effect == "confirm":
        approval_state = "pending_confirmation"
    elif effective_effect in {"not_connected", "unavailable"}:
        approval_state = "unavailable"
    elif effective_effect == "invalid_request":
        approval_state = "invalid_request"
    else:
        approval_state = "denied"
    # The live model must never receive R3/R4 functions before the independent
    # confirmation flow has completed. The voice hold is an execution path,
    # not a reason to advertise a high-risk function to the model.
    # An available R2 function may be advertised while its standing
    # confirmation is pending: the realtime tool call can return the normal
    # confirmation hold without executing anything. R3/R4 functions remain
    # hidden until their independent confirmation is satisfied.
    exposure_allowed = bool(
        not invalid_device
        and availability == "available"
        and (
            allowed
            or (
                confirmation_required
                and decision.effect == "confirm"
                and decision.risk_class == "R2"
            )
        )
        and decision.risk_class not in {"R4", "forbidden"}
    )
    effective_availability = "unavailable" if invalid_device else availability
    return {
        "actor": actor,
        "actor_kind": _actor_kind(actor),
        "device_id": str(device_id) if device_id is not None else None,
        "device_status": device_status or ("bound" if device_id is not None else "unbound"),
        "device_authorized": allowed,
        "authorization": {
            "effect": effective_effect,
            "reason": effective_reason,
            "risk_class": decision.risk_class,
            "provider": decision.provider,
            "required_scopes": list(decision.required_scopes),
        },
        "authorization_effect": effective_effect,
        "authorization_reason": effective_reason,
        "approved": allowed,
        "approval": {
            "state": approval_state,
            "required": confirmation_required,
            "approved": allowed,
            "reason": effective_reason,
            "policy": decision.confirmation_policy,
        },
        "approval_state": approval_state,
        "approval_required": confirmation_required,
        "executable": allowed,
        "executable_reason": effective_reason,
        "confirmation_required": confirmation_required,
        "confirmation_satisfied": confirmation_satisfied,
        "confirmation_state": (
            "satisfied"
            if confirmation_satisfied
            else "required"
            if confirmation_required
            else "not_applicable"
        ),
        "confirmation_policy": decision.confirmation_policy,
        "confirmation_ttl_seconds": decision.confirmation_ttl_seconds,
        "independent_confirmation": decision.independent_confirmation,
        "model_exposed": exposure_allowed,
        "realtime_eligible": exposure_allowed,
        "availability": effective_availability,
        "availability_reason": (
            effective_reason if invalid_device else entry.get("availability_reason")
        ),
    }


def model_surface_mode() -> str:
    """F4 surface control: legacy (48) | shadow (48 + record) | on (reduced)."""

    from app.config import settings

    return (getattr(settings, "model_surface_v2", "legacy") or "legacy").strip().lower()


def f4_surface_filter(names: list[str]) -> list[str]:
    """Apply the F4 model-surface reduction to one projected name list.

    legacy/shadow: unchanged. on: only the target surface survives. The
    filter NEVER widens a surface and NEVER deletes implementations.
    """

    if model_surface_mode() != "on":
        return names
    from app.ev.tool_select import F4_TARGET_SURFACE

    return [name for name in names if name in F4_TARGET_SURFACE]


def live_tool_projection(
    projection: Mapping[str, Any] | Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return only currently exposable entries in ``LIVE_VOICE_TOOLS``."""

    # Lazy import keeps the authority layer independent from voice transport
    # initialization while preserving the one existing live allowlist.
    from app.ev.tool_select import LIVE_VOICE_TOOLS

    if isinstance(projection, Mapping):
        raw_entries = projection.get("live_tool_projection")
        if not isinstance(raw_entries, list):
            raw_entries = projection.get("capabilities")
        if not isinstance(raw_entries, list):
            raw_entries = projection.get("tools") or []
    else:
        raw_entries = projection
    projected: list[dict[str, Any]] = []
    for raw in raw_entries:
        if not isinstance(raw, Mapping):
            continue
        name = str(raw.get("name") or "").strip()
        if name not in LIVE_VOICE_TOOLS:
            continue
        # F4 model-surface reduction (single choke point for every consumer).
        if f4_surface_filter([name]) != [name]:
            continue
        if raw.get("availability") != "available":
            continue
        if raw.get("risk_class") in {"R4", "forbidden"}:
            continue
        if raw.get("model_exposed") is False or raw.get("realtime_eligible") is False:
            continue
        projected.append(dict(raw))
    return sorted(projected, key=lambda item: str(item.get("name") or ""))


def live_realtime_function_tools(
    projection: Mapping[str, Any] | Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Build Realtime function schemas from the live-only projection."""

    return approved_realtime_function_tools(live_tool_projection(projection))


def approved_realtime_function_tools(
    projection: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    allowed_names: set[str] | frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    """Build the approved OpenAI-compatible function tool list from state.

    This helper is pure so transport code can consume it without gaining a
    second capability-selection policy.  A caller may further restrict names
    with ``allowed_names``; it cannot make an unavailable or forbidden entry
    appear.
    """

    if isinstance(projection, Mapping):
        entries = projection.get("live_tool_projection")
        if not isinstance(entries, list):
            entries = projection.get("capabilities")
        if not isinstance(entries, list):
            entries = projection.get("tools") or []
    else:
        entries = projection
    tools: list[dict[str, Any]] = []
    for entry in entries:
        if "availability" in entry and entry.get("availability") != "available":
            continue
        name = str(entry.get("name") or "")
        if not name or (allowed_names is not None and name not in allowed_names):
            continue
        if entry.get("type") != "function" and entry.get("availability") != "available":
            continue
        if entry.get("model_exposed") is False or entry.get("realtime_eligible") is False:
            continue
        if entry.get("risk_class") in {"R4", "forbidden"}:
            continue
        schema = entry.get("json_schema") or entry.get("parameters") or {
            "type": "object",
            "additionalProperties": False,
        }
        tools.append(
            {
                "type": "function",
                "name": name,
                "description": str(entry.get("description") or ""),
                "parameters": schema,
            }
        )
    return sorted(tools, key=lambda item: str(item["name"]))


async def _build_runtime_projection(
    session: AsyncSession,
    *,
    actor: str = "master",
    device_id: UUID | str | None = None,
    realtime_provider: str | None = None,
    channel: str | None = None,
    now: datetime | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Join declarations with current state and actor-specific policy state."""

    clock = now or utcnow()
    actor_name = str(actor or "master")
    devices = list(
        (
            await session.execute(
                select(Device)
                .where(Device.revoked_at.is_(None))
                .order_by(Device.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    device_payloads = [_device_payload(device, now=clock) for device in devices]
    selected_device: dict[str, Any] | None = None
    requested_device_status: str | None = None
    if device_id is not None:
        requested = str(device_id)
        selected_device = next(
            (item for item in device_payloads if item["id"] == requested or item["name"] == requested),
            None,
        )
        if selected_device is None:
            requested_device_status = "unknown"
    active_integrations = list(
        (
            await session.execute(
                select(Integration)
                .where(Integration.status == "active")
                .order_by(Integration.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    provider_rows: dict[str, Integration] = {}
    for integration in active_integrations:
        provider_rows.setdefault(str(integration.adapter), integration)
    credential_ids = set(
        (
            await session.execute(
                select(IntegrationCredential.integration_id).where(
                    IntegrationCredential.kind == "oauth",
                    IntegrationCredential.revoked_at.is_(None),
                    IntegrationCredential.encrypted_access.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    camera_available = (
        await session.execute(select(OwnerCamera.id).limit(1))
    ).scalar_one_or_none() is not None

    bound_device_id: UUID | None = None
    if selected_device is not None:
        bound_device_id = UUID(str(selected_device["id"]))
    elif isinstance(device_id, UUID):
        bound_device_id = device_id

    entries: list[dict[str, Any]] = []
    for spec in _declared_specs():
        entry = _entry(
            spec,
            selected_device=selected_device,
            available_devices=device_payloads,
            provider_rows=provider_rows,
            credentialed_integrations=credential_ids,
            camera_available=camera_available,
        )
        entry.update(
            await _authorization_state(
                session,
                entry,
                spec,
                actor=actor_name,
                device_id=bound_device_id,
                channel=channel,
                now=clock,
                device_status_override=requested_device_status,
            )
        )
        if entry.get("availability") != "available":
            entry["fallback_reason"] = str(entry.get("availability_reason") or entry.get("fallback"))
        elif entry.get("confirmation_required") is True:
            entry["fallback_reason"] = (
                "confirmation required: "
                + str(entry.get("confirmation_policy") or "independent confirmation")
            )
        elif entry.get("executable") is not True:
            entry["fallback_reason"] = str(
                entry.get("executable_reason") or entry.get("authorization_reason") or "not executable"
            )
        else:
            from app.ev.tool_select import LIVE_VOICE_TOOLS

            entry["fallback_reason"] = (
                None
                if str(entry.get("name") or "") in LIVE_VOICE_TOOLS
                else "not in LIVE_VOICE_TOOLS"
            )
        entries.append(entry)

    import app.ev.turn_capability  # noqa: F401  # G1.3: registers evie.turn_controller/state/manager
    import app.life.capability  # noqa: F401  # G1: registers life_state projector
    from app.ev.apps import find_macos_life_integration
    from app.ev.camera_runtime import readiness_from_camera_state
    from app.ev.capability_registry import apply_capability_overlays
    from app.ev.computer_runtime import readiness_from_computer_state
    from app.voice.live.layer import live_for_device, live_for_session

    live = live_for_session(str(session_id) if session_id else None) or live_for_device(
        str(bound_device_id or device_id or "") or None
    )
    camera_state = dict(getattr(live, "_camera_state", {}) or {}) if live is not None else {}
    camera_readiness = readiness_from_camera_state(
        camera_state,
        client_connected=live is not None,
        realtime_provider=realtime_provider,
        device_id=str(bound_device_id or device_id or "") or None,
        session_id=str(session_id) if session_id else None,
        connecting_device=bool(bound_device_id or device_id) and live is None,
    )
    if live is not None:
        camera_readiness.last_capture_status = getattr(live, "_last_capture_status", None)

    helper_ready = False
    try:
        helper_ready = await find_macos_life_integration(session) is not None
    except Exception:  # noqa: BLE001 - overlay must not fail the manifest
        helper_ready = False
    computer_state = dict(getattr(live, "_computer_state", {}) or {}) if live is not None else {}
    computer_readiness = readiness_from_computer_state(
        computer_state,
        client_connected=live is not None,
        helper_ready=helper_ready,
        realtime_provider=realtime_provider,
        device_id=str(bound_device_id or device_id or "") or None,
        session_id=str(session_id) if session_id else None,
        realtime_session_connected=bool(
            live is not None
            and getattr(getattr(live, "grok_voice", None), "upstream_session_ready", False)
        ),
        provider_tools_confirmed=bool(
            (getattr(getattr(live, "grok_voice", None), "realtime_diagnostics", {}) or {}).get(
                "provider_tools_confirmed"
            )
        ),
        tool_schema_match=bool(
            (getattr(getattr(live, "grok_voice", None), "realtime_diagnostics", {}) or {}).get(
                "tool_schema_match"
            )
        ),
        computer_tool_schema_hash=(
            (getattr(getattr(live, "grok_voice", None), "realtime_diagnostics", {}) or {}).get(
                "computer_tool_schema_hash"
            )
        ),
    )
    # G1 core state has no external dependency to probe: canonical tables are
    # the readiness signal. Overlay stamps every life_* / mission_control entry.
    from app.ev.turn_capability import MANAGER_TOOLS
    from app.ev.turn_capability import STATE_TOOLS as EVIE_STATE_TOOLS
    from app.ev.turn_capability import TURN_TOOLS as TURN_CTRL_TOOLS
    from app.life.capability import LIFE_TOOLS

    life_readiness = {"ready": True, "tools": sorted(LIFE_TOOLS)}
    # G1.3 Turn Controller / State / Manager
    from app.ev.model_router import manager_model_info, turn_control_model_info

    turn_info = turn_control_model_info()
    # Turn controller is always ready via rule fallback; availability tracked in health
    turn_readiness = {"ready": True, "tools": sorted(TURN_CTRL_TOOLS), "available": turn_info.available}
    state_readiness = {"ready": True, "tools": sorted(EVIE_STATE_TOOLS)}
    mgr = manager_model_info()
    manager_status = "scaffolded" if mgr.available else "not_active"
    manager_readiness = {"ready": manager_status in ("scaffolded", "available"), "tools": sorted(MANAGER_TOOLS), "status": manager_status}
    entries = apply_capability_overlays(
        entries,
        {
            "camera": camera_readiness,
            "computer": computer_readiness,
            "life_state": life_readiness,
            "evie.turn_controller": turn_readiness,
            "evie.state": state_readiness,
            "evie.manager": manager_readiness,
        },
    )

    all_realtime_tools = approved_realtime_function_tools(entries)
    live_entries = live_tool_projection(entries)
    realtime_tools = approved_realtime_function_tools(live_entries)
    # G1.6 cutover: TurnGate owns life-state, Realtime must not have direct state tools
    if getattr(settings, "turn_gate_enabled", False):
        gate_excluded = {
            "life_project_create", "life_project_update", "life_project_query",
            "life_goal_create", "life_goal_update", "life_goal_add_step", "life_goal_query",
            "life_commitment_create", "life_commitment_update", "life_commitment_query",
            "life_relationship_set", "mission_control", "evie_turn",
        }
        live_entries = [e for e in live_entries if e.get("name") not in gate_excluded]
        realtime_tools = [t for t in realtime_tools if t.get("name") not in gate_excluded]
        all_realtime_tools = [t for t in all_realtime_tools if t.get("name") not in gate_excluded]
    # G1.6 cutover: TurnGate owns life-state, Realtime must not have direct state tools
    if getattr(settings, "turn_gate_enabled", False):
        gate_excluded = {
            "life_project_create", "life_project_update", "life_project_query",
            "life_goal_create", "life_goal_update", "life_goal_add_step", "life_goal_query",
            "life_commitment_create", "life_commitment_update", "life_commitment_query",
            "life_relationship_set", "mission_control", "evie_turn",
        }
        live_entries = [e for e in live_entries if e.get("name") not in gate_excluded]
        realtime_tools = [t for t in realtime_tools if t.get("name") not in gate_excluded]
        all_realtime_tools = [t for t in all_realtime_tools if t.get("name") not in gate_excluded]
    providers = {
        provider: {
            "provider": provider,
            "adapter": row.adapter,
            "name": row.name,
            "scopes": list(row.scopes or []),
            "status": row.status,
        }
        for provider, row in provider_rows.items()
    }
    projected_tool_names = [str(item["name"]) for item in live_entries]
    executable_tool_names = [
        str(item["name"])
        for item in live_entries
        if item.get("executable") is True
    ]
    confirmation_required_tool_names = [
        str(item["name"]) for item in entries if item.get("confirmation_required") is True
    ]
    unavailable_tool_names = [
        str(item["name"])
        for item in entries
        if item.get("availability") != "available"
    ]
    not_exposed_tool_names = [
        str(item["name"])
        for item in entries
        if item.get("model_exposed") is not True
    ]
    projection_device_id = (
        str(bound_device_id) if bound_device_id is not None else
        str(device_id) if device_id is not None else None
    )
    capability_diagnostics = {
        "session_id": str(session_id) if session_id is not None else None,
        "actor": actor_name,
        "device_id": projection_device_id,
        "provider": realtime_provider,
        "capability_error": None,
        "projected_tool_names": projected_tool_names,
        "executable_tool_names": executable_tool_names,
        "confirmation_required_tool_names": confirmation_required_tool_names,
        "unavailable_tool_names": unavailable_tool_names,
        "not_exposed_tool_names": not_exposed_tool_names,
        "projection_timestamp": clock.isoformat(),
    }
    logger.info(
        "live capability projection session=%s actor=%s device_id=%s provider=%s projected=%s executable=%s confirmation=%s unavailable=%s",
        capability_diagnostics["session_id"],
        actor_name,
        capability_diagnostics["device_id"],
        realtime_provider,
        projected_tool_names,
        executable_tool_names,
        confirmation_required_tool_names,
        unavailable_tool_names,
    )
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generated_at": clock.isoformat(),
        "projection_timestamp": clock.isoformat(),
        "session_id": capability_diagnostics["session_id"],
        "actor": actor_name,
        "actor_kind": _actor_kind(actor_name),
        "device_id": projection_device_id,
        "realtime_provider": realtime_provider,
        "device": selected_device,
        "current_device": selected_device,
        "devices": device_payloads,
        "providers": providers,
        "current_provider": realtime_provider,
        "provider": realtime_provider,
        "capabilities": entries,
        # ``tools`` is the live voice surface, deliberately narrower than the
        # full registry. The full approved projection remains available under
        # ``all_realtime_tools`` for non-voice clients.
        "tools": realtime_tools,
        "live_tool_projection": live_entries,
        "live_tools": realtime_tools,
        "all_realtime_tools": all_realtime_tools,
        "realtime": {
            "provider": realtime_provider,
            "tools": realtime_tools,
            "tool_choice": "auto",
        },
        "realtime_tools": realtime_tools,
        "approved_tools": projected_tool_names,
        "executable_tools": executable_tool_names,
        "confirmation_tools": confirmation_required_tool_names,
        "projected_tool_names": projected_tool_names,
        "executable_tool_names": executable_tool_names,
        "confirmation_required_tool_names": confirmation_required_tool_names,
        "unavailable_tool_names": unavailable_tool_names,
        "not_exposed_tool_names": not_exposed_tool_names,
        "capability_error": None,
        "diagnostics": capability_diagnostics,
        "camera": camera_readiness.as_dict(),
        "computer_control": computer_readiness.as_dict(),
    }


def _safe_capability_error(exc: BaseException) -> str:
    detail = " ".join(str(exc).split())[:240]
    detail = re.sub(
        r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|authorization)\s*[=:]\s*[^\s,;]+",
        r"\1=<redacted>",
        detail,
    )
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


async def build_runtime_projection(
    session: AsyncSession,
    *,
    actor: str = "master",
    device_id: UUID | str | None = None,
    realtime_provider: str | None = None,
    channel: str | None = None,
    now: datetime | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Build a fail-closed projection while keeping the failure inspectable."""

    try:
        return await _build_runtime_projection(
            session,
            actor=actor,
            device_id=device_id,
            realtime_provider=realtime_provider,
            channel=channel,
            now=now,
            session_id=session_id,
        )
    except Exception as exc:  # noqa: BLE001 - diagnostics must preserve fail-closed state
        error = _safe_capability_error(exc)
        clock = now or utcnow()
        actor_name = str(actor or "master")
        device_value = str(device_id) if device_id is not None else None
        diagnostics: dict[str, Any] = {
            "session_id": str(session_id) if session_id is not None else None,
            "actor": actor_name,
            "device_id": device_value,
            "provider": realtime_provider,
            "capability_error": error,
            "projected_tool_names": [],
            "executable_tool_names": [],
            "confirmation_required_tool_names": [],
            "unavailable_tool_names": [],
            "not_exposed_tool_names": [],
            "projection_timestamp": clock.isoformat(),
        }
        logger.error(
            "live capability projection failed session=%s actor=%s device_id=%s provider=%s error=%s",
            diagnostics["session_id"],
            actor_name,
            device_value,
            realtime_provider,
            error,
        )
        return {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "generated_at": clock.isoformat(),
            "projection_timestamp": clock.isoformat(),
            "session_id": diagnostics["session_id"],
            "actor": actor_name,
            "actor_kind": _actor_kind(actor_name),
            "device_id": device_value,
            "realtime_provider": realtime_provider,
            "provider": realtime_provider,
            "device": None,
            "current_device": None,
            "devices": [],
            "providers": {},
            "current_provider": realtime_provider,
            "capabilities": [],
            "tools": [],
            "live_tool_projection": [],
            "live_tools": [],
            "all_realtime_tools": [],
            "realtime": {"provider": realtime_provider, "tools": [], "tool_choice": "auto"},
            "realtime_tools": [],
            "approved_tools": [],
            "executable_tools": [],
            "confirmation_tools": [],
            "projected_tool_names": [],
            "executable_tool_names": [],
            "confirmation_required_tool_names": [],
            "unavailable_tool_names": [],
            "not_exposed_tool_names": [],
            "capability_error": error,
            "diagnostics": diagnostics,
            "camera": {},
            "computer_control": {},
        }


__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "RUNTIME_FIELDS",
    "approved_realtime_function_tools",
    "build_runtime_projection",
    "live_realtime_function_tools",
    "live_tool_projection",
]
