"""Shared Permissioned Operating Layer authority path.

This is not a fourth registry. Capability names, schemas, and permissions stay
in ``TOOL_SPECS``, ``ACTION_SPECS``, ``FLEET_TOOL_SPECS``, and
``IntegrationRegistry``. ``evaluate_policy()`` is the one deterministic decision
used by voice tool dispatch, HTTP/action routing, background jobs, and
device-triggered actions. Model confidence is never an authorization input.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device, Integration, OwnerCamera
from app.utils.text import utcnow

RiskClass = Literal["R0", "R1", "R2", "R3", "R4", "forbidden"]
# Canonical effects. ``refuse`` is the permanent-ban form of deny. ``reject`` is
# kept as an alias of ``invalid_request`` for existing callers.
PolicyEffect = Literal[
    "allow",
    "deny",
    "confirm",
    "not_connected",
    "invalid_request",
    "unavailable",
    "refuse",
    "reject",
]
AuthChannel = Literal["voice", "action"]
ActorKind = Literal["owner", "share", "device", "worker", "model"]
TargetStatus = Literal["owned", "shared", "public", "unknown", "unowned", "ambiguous", "missing"]
DeviceStatus = Literal["ok", "revoked", "unknown", "untrusted"]

CAPABILITY_VERSION = "1"
CAPABILITY_CONTRACT_FIELDS = (
    "name",
    "version",
    "description",
    "parameters",
    "output",
    "required_scopes",
    "risk_class",
    "confirmation",
    "target_ownership",
    "provider",
    "fallback",
    "evidence",
    "idempotency",
    "timeout_seconds",
    "cancellation",
    "audit_event",
)

TIMEOUT_BY_RISK: dict[str, int] = {
    "R0": 10,
    "R1": 10,
    "R2": 15,
    "R3": 30,
    "R4": 30,
    "forbidden": 0,
}

IGNORED_ARGUMENT_KEYS = frozenset({"confidence"})
WORKER_ACTORS = frozenset({"worker", "scheduler", "job"})
MODEL_ACTORS = frozenset({"model", "llm", "assistant"})
PHASE0_CAPABILITIES = frozenset(
    {"get_weather", "calibrate", "calendar_read", "list_messages"}
)

# Capabilities whose dispatch/route path must honor evaluate_policy() for
# provider honesty, evidence, scopes, and confirmation. Phase 0 reads plus
# Phase 3/4 life I/O and the one physical light actuator.
ROUTED_CAPABILITIES = frozenset(
    {
        "get_weather",
        "search_web",
        "web_search",
        "research",
        "public_lookup",
        "calibrate",
        "calendar_read",
        "list_messages",
        "resolve_contact",
        "send_message",
        "present",
        "execute_command",
        "start_timer",
        "set_reminder",
        "cancel_timer",
        "list_timers",
        "snooze_timer",
        "calendar_add",
        "place_call",
        "open_url",
        "open_app",
        "close_app",
        "list_mail",
        "draft_reply",
        "whats_on_my_plate",
        "home_act",
        "home_status",
        "print_start",
        "estimate_print",
        "camera_replay",
        "look",
        "drone",
        "media_check",
        "estimate_structure",
    }
)

CONFIRMATION_NONE = "none"
CONFIRMATION_STANDING = "standing"
CONFIRMATION_FRESH = "fresh"
CONFIRMATION_REFUSE = "refuse"

INDEPENDENT_FACTORS = frozenset(
    {"hud", "biometric", "reverify", "master_key", "http_approve", "webauthn"}
)
VOICE_FACTORS = frozenset({"voice", "voice_wake", "speaker_verified"})
OWNER_ACTORS = frozenset({"master", "voice", "owner"})

R3_TTL_SECONDS = 120
R4_TTL_SECONDS = 60

HOLD_LINE = "I have the request ready; confirm it on your phone."

FORBIDDEN_NAMES = frozenset(
    {
        "instant_kill",
        "launch_nukes",
        "wiretap",
        "telecom_wiretap",
        "city_facial_hunt",
        "satellite_drone_weapons",
        "become_vision",
        "stranger_baby_monitor",
    }
)

R4_PERMISSIONS = frozenset({"ticket:buy", "shell:execute"})
R4_NAMES = frozenset({"ticket_buy", "execute_command"})
R3_PERMISSIONS = frozenset(
    {
        "phone:act",
        "home:act",
        "camera:read",
        "drone:act",
        "actuator:drone",
        "printer:act",
    }
)
# delegate_grant is a read-only scoped share (R2 standing), not a physical actuator.
R3_NAMES = frozenset({"place_call", "home_act", "drone", "print_start", "camera_replay"})

# Mirrors training_wheels.PERMISSION_GATES. Policy refuses these until TW completes;
# dispatch still runs refuse_if_locked first so the error code stays training_wheels.
TW_GATED_PERMISSIONS = frozenset(
    {
        "phone:act",
        "actuator:software",
        "home:act",
        "actuator:drone",
        "maker:queue",
    }
)

SCOPE_ALIASES: dict[str, frozenset[str]] = {
    "message:read": frozenset({"message:read", "messaging:read"}),
    "messaging:read": frozenset({"message:read", "messaging:read"}),
    "message:send": frozenset({"message:send", "messaging:act"}),
    "messaging:act": frozenset({"message:send", "messaging:act"}),
    "web:search": frozenset({"web:search"}),
    "diagnostics:read": frozenset({"diagnostics:read"}),
    "calendar:read": frozenset({"calendar:read"}),
    "calendar:write": frozenset({"calendar:write", "calendar:act"}),
    "calendar:act": frozenset({"calendar:write", "calendar:act"}),
    "mail:read": frozenset({"mail:read"}),
    "mail:write": frozenset({"mail:write", "mail:act"}),
    "mail:act": frozenset({"mail:write", "mail:act"}),
    "home:act": frozenset({"home:act"}),
    "home:read": frozenset({"home:read"}),
    "life:open_url": frozenset({"life:open_url", "apps:act"}),
    "apps:act": frozenset({"apps:act", "life:open_url"}),
    "phone:act": frozenset({"phone:act"}),
    "life:read": frozenset({"life:read", "calendar:read", "mail:read", "github:read"}),
}

PROVIDER_SLUGS: dict[str, str] = {
    "get_weather": "open-meteo",
    "search_web": "search",
    "web_search": "search",
    "calibrate": "local",
    "calendar_read": "calendar",
    "list_messages": "messaging",
    "resolve_contact": "contacts",
    "send_message": "messaging",
    "start_timer": "local",
    "set_reminder": "local",
    "cancel_timer": "local",
    "list_timers": "local",
    "snooze_timer": "local",
    "calendar_add": "calendar",
    "place_call": "phone",
    "open_url": "macos_life",
    "open_app": "macos_life",
    "close_app": "macos_life",
    "camera_replay": "camera",
    "look": "vision",
    "list_mail": "mail",
    "draft_reply": "local",
    "whats_on_my_plate": "local",
    "home_act": "smart_home",
    "home_status": "local",
}

# Public/local providers are not Integration rows. Missing Integration is
# not_connected only for these slugs. smart_home stays off this list so the
# labeled local house double remains the CI path; Home Assistant misconfig
# is reported by the adapter, not by inventing success.
INTEGRATION_PROVIDERS = frozenset({"calendar", "messaging", "phone", "mail", "contacts"})


@dataclass(frozen=True)
class Confirmation:
    """Fresh, target-bound approval presented to policy."""

    factor: str
    confirmed: bool = True
    target: str | None = None
    expires_at: datetime | None = None
    session_id: str | None = None
    issued_at: datetime | None = None


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    effect: PolicyEffect
    reason: str
    risk_class: RiskClass
    required_scopes: tuple[str, ...] = ()
    confirmation_required: bool = False
    confirmation_policy: str = CONFIRMATION_NONE
    independent_confirmation: bool = False
    confirmation_ttl_seconds: int | None = None
    target: str | None = None
    target_ownership: str = "owner"
    provider: str | None = None
    evidence_fields: tuple[str, ...] = ("source", "timestamp")
    spoken: str | None = None
    routed: bool = False
    audit: dict[str, Any] = field(default_factory=dict)

    def to_result(self) -> dict[str, Any]:
        """Honest adapter-shaped payload for a denied or pending decision."""

        payload = {
            "ok": False,
            "error": self.effect if self.effect != "deny" else self.reason,
            "reason": self.reason,
            "risk_class": self.risk_class,
            "confirmation_required": self.confirmation_required,
            "independent_confirmation": self.independent_confirmation,
            "target": self.target,
            "provider": self.provider,
        }
        if self.confirmation_ttl_seconds is not None:
            payload["ttl_seconds"] = self.confirmation_ttl_seconds
        if self.independent_confirmation:
            payload["confirmation_channel"] = "hud_or_biometric"
        if self.spoken:
            payload["spoken"] = self.spoken
        if self.effect == "not_connected":
            payload["error"] = "not_connected"
            payload["degraded"] = True
        if self.effect == "refuse":
            payload["error"] = "refused"
        if self.effect == "reject":
            payload["error"] = "unknown_capability"
        if self.effect == "confirm":
            payload["error"] = "confirmation_required"
            payload["needs_confirm"] = True
        return payload


def infer_channel(actor: str, channel: str | None = None) -> AuthChannel:
    if channel == "voice":
        return "voice"
    if channel == "action":
        return "action"
    return "voice" if actor == "voice" else "action"


def canonical_target(name: str, arguments: dict | None) -> str | None:
    args = arguments or {}
    for key in (
        "name",
        "to",
        "destination",
        "entity",
        "place",
        "query",
        "channel",
        "title",
        "id",
        "mail_id",
        "camera",
        "project",
        "command",
        "url",
        "app",
    ):
        value = args.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    if name in ROUTED_CAPABILITIES:
        if name == "get_weather":
            return "home"
        if name == "calendar_read":
            return "owner"
        if name == "list_messages":
            return str(args.get("channel") or "messages")
        if name == "calibrate":
            return "local"
    return None


def confirmation_policy_for(risk_class: RiskClass) -> str:
    if risk_class == "forbidden":
        return CONFIRMATION_REFUSE
    if risk_class in {"R3", "R4"}:
        return CONFIRMATION_FRESH
    if risk_class == "R2":
        return CONFIRMATION_STANDING
    return CONFIRMATION_NONE


def ttl_for(risk_class: RiskClass) -> int | None:
    if risk_class == "R4":
        return R4_TTL_SECONDS
    if risk_class == "R3":
        return R3_TTL_SECONDS
    return None


def derive_risk_class(spec: dict[str, Any] | None, name: str) -> RiskClass:
    if name in FORBIDDEN_NAMES:
        return "forbidden"
    if spec is None:
        return "forbidden"
    declared = spec.get("risk_class")
    if declared in {"R0", "R1", "R2", "R3", "R4", "forbidden"}:
        return declared
    permission = str(spec.get("permission") or "")
    if permission in R4_PERMISSIONS or name in R4_NAMES:
        return "R4"
    if permission in R3_PERMISSIONS or name in R3_NAMES:
        return "R3"
    if spec.get("sensitive") or spec.get("requires_approval"):
        return "R2"
    if spec.get("read_only"):
        return "R0"
    if spec.get("undoable"):
        return "R1"
    return "R2"


def annotate_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Fill POL fields from existing permission/read_only/sensitive. Not a registry."""

    out = dict(spec)
    name = str(out.get("name") or "")
    if "parameters" not in out and out.get("payload") is not None:
        out["parameters"] = out["payload"]
    risk = derive_risk_class(out, name)
    out.setdefault("version", CAPABILITY_VERSION)
    out.setdefault("risk_class", risk)
    out.setdefault("confirmation", confirmation_policy_for(risk))
    if name == "get_weather":
        out.setdefault("target_ownership", "public")
    else:
        out.setdefault("target_ownership", "owner")
    out.setdefault("provider", PROVIDER_SLUGS.get(name) or "local")
    out.setdefault("evidence", ["source", "timestamp"])
    provider = str(out.get("provider") or "local")
    if provider in INTEGRATION_PROVIDERS:
        out.setdefault("fallback", "not_connected: connect the provider and grant the required scope")
    elif provider == "smart_home":
        out.setdefault("fallback", "not_connected: configure the local or Home Assistant bridge")
    elif provider in {"local", "open-meteo", "vision"}:
        out.setdefault("fallback", "report unavailable; do not fabricate success")
    else:
        out.setdefault("fallback", "unavailable: provider adapter is not configured")
    if risk in {"R0", "R1"}:
        out.setdefault("idempotency", "natural")
        out.setdefault("cancellation", "not_applicable")
    elif risk == "R2":
        out.setdefault("idempotency", "key")
        out.setdefault("cancellation", "cooperative")
    else:
        out.setdefault("idempotency", "key")
        out.setdefault("cancellation", "required")
    out.setdefault("timeout_seconds", TIMEOUT_BY_RISK.get(risk, TIMEOUT_BY_RISK["R2"]))
    out.setdefault("audit_event", f"capability.{name}")
    if "required_scopes" not in out and out.get("permission"):
        out["required_scopes"] = [str(out["permission"])]
    return out


def required_scopes_for(spec: dict[str, Any] | None) -> tuple[str, ...]:
    if spec is None:
        return ()
    declared = spec.get("required_scopes")
    if isinstance(declared, (list, tuple)) and declared:
        return tuple(str(item) for item in declared)
    permission = spec.get("permission")
    return (str(permission),) if permission else ()


def resolve_capability(name: str) -> dict[str, Any] | None:
    """Look up an existing tool or action spec. Not a new registry."""

    from app.ev.tools import get_spec

    spec = get_spec(name)
    if spec is not None:
        return spec
    from app.ev.actions import get_action_spec

    return get_action_spec(name)


def _scope_match(required: str, granted: Sequence[str]) -> bool:
    granted_set = {str(item) for item in granted}
    aliases = SCOPE_ALIASES.get(required, frozenset({required}))
    return bool(granted_set & aliases) or required in granted_set


def _confirmation_valid(
    confirmation: Confirmation | None,
    *,
    target: str | None,
    now: datetime,
    ttl: int | None,
    risk_class: RiskClass,
    session_id: str | None = None,
) -> bool:
    if confirmation is None or not confirmation.confirmed:
        return False
    expires_at = confirmation.expires_at
    issued = confirmation.issued_at
    # Persisted SQLite timestamps may be naive while live confirmations are
    # timezone-aware. Normalize both forms before applying expiry/TTL checks.
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=now.tzinfo)
    if issued is not None and issued.tzinfo is None:
        issued = issued.replace(tzinfo=now.tzinfo)
    if expires_at is not None and expires_at <= now:
        return False
    if ttl and issued is not None and now - issued >= timedelta(seconds=ttl):
        return False
    if session_id and confirmation.session_id and _norm(confirmation.session_id) != _norm(session_id):
        return False
    if risk_class in {"R3", "R4"} and target:
        if not confirmation.target or _norm(confirmation.target) != _norm(target):
            return False
    elif target and confirmation.target and _norm(confirmation.target) != _norm(target):
        return False
    if risk_class not in {"R3", "R4"}:
        return True
    if confirmation.factor in VOICE_FACTORS:
        return False
    return confirmation.factor in INDEPENDENT_FACTORS


def _norm(value: str) -> str:
    return str(value).strip().lower()


def evaluate_policy(
    name: str,
    *,
    spec: dict[str, Any] | None = None,
    actor: str = "master",
    channel: AuthChannel | str | None = None,
    arguments: dict | None = None,
    granted_scopes: Sequence[str] | None = None,
    provider_scopes: Sequence[str] | None = None,
    confirmation: Confirmation | None = None,
    training_wheels_complete: bool = True,
    provider_connected: bool | None = True,
    target_ownership: str | None = None,
    owner_trusted: bool | None = None,
    device_status: DeviceStatus | None = None,
    standing_owner_scope: bool = False,
    session_id: str | None = None,
    now: datetime | None = None,
) -> PolicyDecision:
    """Deterministic allow / confirm / refuse decision. The model cannot override it.

    ``arguments["confidence"]`` is ignored. Model-reported confidence is never
    an authorization input.
    """

    resolved = spec if spec is not None else resolve_capability(name)
    auth_channel = infer_channel(actor, channel)
    clock = now or utcnow()
    risk = derive_risk_class(resolved, name)
    trusted = actor in OWNER_ACTORS if owner_trusted is None else bool(owner_trusted)
    # A model may propose an intent, but it can never inherit owner authority
    # from a caller-provided boolean. Device trust is checked by ``authorize``
    # and this predicate also makes direct policy calls fail closed.
    if actor in MODEL_ACTORS:
        trusted = False
    scopes = required_scopes_for(resolved)
    confirm_policy = resolved.get("confirmation") if resolved else None
    if confirm_policy not in {
        CONFIRMATION_NONE,
        CONFIRMATION_STANDING,
        CONFIRMATION_FRESH,
        CONFIRMATION_REFUSE,
    }:
        confirm_policy = confirmation_policy_for(risk)
    ownership = str(
        target_ownership
        or (resolved.get("target_ownership") if resolved else None)
        or ("public" if name == "get_weather" else "owner")
    )
    provider = None
    if resolved and resolved.get("provider"):
        provider = str(resolved["provider"])
    else:
        provider = PROVIDER_SLUGS.get(name)
    declared_evidence = (resolved.get("evidence") if resolved else None) or ("source", "timestamp")
    if isinstance(declared_evidence, str):
        declared_evidence = (declared_evidence,)
    evidence = tuple(declared_evidence)
    target = canonical_target(name, arguments)
    ttl = ttl_for(risk)
    routed = name in ROUTED_CAPABILITIES
    audit = {
        "name": name,
        "actor": actor,
        "channel": auth_channel,
        "risk_class": risk,
        "routed": routed,
        "training_wheels_complete": training_wheels_complete,
        "standing_owner_scope": standing_owner_scope,
        "owner_trusted": trusted,
        "confidence_ignored": True,
    }

    if resolved is None or name in FORBIDDEN_NAMES:
        if resolved is None and name not in FORBIDDEN_NAMES:
            return PolicyDecision(
                allowed=False,
                effect="reject",
                reason=f"unknown capability '{name}'",
                risk_class="forbidden",
                confirmation_policy=CONFIRMATION_REFUSE,
                spoken="I don't have that capability.",
                audit=audit,
            )
        return PolicyDecision(
            allowed=False,
            effect="refuse",
            reason="forbidden capability",
            risk_class="forbidden",
            confirmation_policy=CONFIRMATION_REFUSE,
            spoken="I will not do that.",
            routed=routed,
            audit=audit,
        )

    if device_status in {"revoked", "unknown", "untrusted"}:
        return PolicyDecision(
            allowed=False,
            effect="deny",
            reason=f"invalid actor/device combination: {device_status} device",
            risk_class=risk,
            required_scopes=scopes,
            confirmation_policy=confirm_policy,
            target=target,
            target_ownership=ownership,
            provider=provider,
            evidence_fields=evidence,
            spoken="That device is not authorized for this capability.",
            routed=routed,
            audit={**audit, "device_status": device_status},
        )

    # Policy owns schema validation as well as dispatch validation. This keeps
    # background jobs, HTTP, voice, and device callers on the same rejection
    # path. ``confidence`` is deliberately discarded: it is model metadata,
    # never an authorization input.
    from app.gateway.validation import validate_arguments

    raw_arguments = dict(arguments or {})
    validation_arguments = {
        key: value for key, value in raw_arguments.items() if key not in IGNORED_ARGUMENT_KEYS
    }
    parameters = (resolved.get("parameters") or resolved.get("payload") or {}) if resolved else {}
    _effective_arguments, argument_issues = validate_arguments(
        validation_arguments,
        parameters,
    )
    if argument_issues:
        return PolicyDecision(
            allowed=False,
            effect="invalid_request",
            reason="invalid arguments: " + "; ".join(argument_issues),
            risk_class=risk,
            required_scopes=scopes,
            confirmation_policy=confirm_policy,
            target=target,
            target_ownership=ownership,
            provider=provider,
            evidence_fields=evidence,
            spoken="That request has invalid arguments.",
            routed=routed,
            audit={**audit, "argument_issues": argument_issues},
        )

    if ownership in {"ambiguous", "missing", "unknown"}:
        return PolicyDecision(
            allowed=False,
            effect="invalid_request",
            reason=f"{ownership} target",
            risk_class=risk,
            required_scopes=scopes,
            confirmation_policy=confirm_policy,
            target=target,
            target_ownership=ownership,
            provider=provider,
            evidence_fields=evidence,
            spoken="Tell me exactly which target you mean.",
            routed=routed,
            audit={**audit, "target_status": ownership},
        )
    if ownership == "unowned":
        return PolicyDecision(
            allowed=False,
            effect="deny",
            reason="target is not owned or explicitly shared",
            risk_class=risk,
            required_scopes=scopes,
            confirmation_policy=confirm_policy,
            target=target,
            target_ownership=ownership,
            provider=provider,
            evidence_fields=evidence,
            spoken="I won't act on an unowned target.",
            routed=routed,
            audit={**audit, "target_status": ownership},
        )
    if ownership == "shared" and (risk != "R0" or not bool(resolved.get("read_only"))):
        return PolicyDecision(
            allowed=False,
            effect="deny",
            reason="scoped shares are read-only",
            risk_class=risk,
            required_scopes=scopes,
            confirmation_policy=confirm_policy,
            target=target,
            target_ownership=ownership,
            provider=provider,
            evidence_fields=evidence,
            spoken="That shared scope is read-only.",
            routed=routed,
            audit={**audit, "target_status": ownership},
        )

    if risk in {"R3", "R4"} and not target:
        return PolicyDecision(
            allowed=False,
            effect="invalid_request",
            reason="high-risk capability requires a target",
            risk_class=risk,
            required_scopes=scopes,
            confirmation_required=True,
            confirmation_policy=CONFIRMATION_FRESH,
            independent_confirmation=True,
            confirmation_ttl_seconds=ttl,
            target=None,
            target_ownership=ownership,
            provider=provider,
            evidence_fields=evidence,
            spoken="I need a specific target before I can prepare that action.",
            routed=routed,
            audit={**audit, "target_required": True},
        )

    permission = str((resolved or {}).get("permission") or "")
    # A device-proxy enqueue is an approved, target-bound handoff to a
    # registered device; the device still has to claim and deliver it before
    # any actuator runs. Keep the R3 confirmation/scope checks, but do not
    # confuse queue persistence with the physical actuation gate.
    explicit_owner_confirmation = bool(
        trusted
        and confirmation is not None
        and confirmation.confirmed
        and confirmation.factor in INDEPENDENT_FACTORS
    )
    if (
        not training_wheels_complete
        and permission in TW_GATED_PERMISSIONS
        and not bool((resolved or {}).get("queue_only"))
        and not explicit_owner_confirmation
    ):
        return PolicyDecision(
            allowed=False,
            effect="deny",
            reason="training_wheels",
            risk_class=risk,
            required_scopes=scopes,
            confirmation_policy=confirm_policy,
            target=target,
            target_ownership=ownership,
            provider=provider,
            evidence_fields=evidence,
            spoken="Finish Training Wheels first.",
            routed=routed,
            audit=audit,
        )

    if ownership == "unknown" and risk in {"R2", "R3", "R4"}:
        return PolicyDecision(
            allowed=False,
            effect="deny",
            reason="unknown target ownership",
            risk_class=risk,
            required_scopes=scopes,
            confirmation_policy=confirm_policy,
            target=target,
            target_ownership=ownership,
            provider=provider,
            evidence_fields=evidence,
            spoken="I won't act on an unknown target.",
            routed=routed,
            audit=audit,
        )

    if granted_scopes is not None:
        missing = [scope for scope in scopes if not _scope_match(scope, granted_scopes)]
        if missing:
            return PolicyDecision(
                allowed=False,
                effect="deny",
                reason=f"missing scopes: {', '.join(missing)}",
                risk_class=risk,
                required_scopes=scopes,
                confirmation_policy=confirm_policy,
                target=target,
                target_ownership=ownership,
                provider=provider,
                evidence_fields=evidence,
                spoken="That isn't in the granted scopes.",
                routed=routed,
                audit={**audit, "missing_scopes": missing},
            )
    if provider_scopes is not None:
        missing_provider = [scope for scope in scopes if not _scope_match(scope, provider_scopes)]
        if missing_provider:
            return PolicyDecision(
                allowed=False,
                effect="deny",
                reason=f"provider missing scopes: {', '.join(missing_provider)}",
                risk_class=risk,
                required_scopes=scopes,
                confirmation_policy=confirm_policy,
                target=target,
                target_ownership=ownership,
                provider=provider,
                evidence_fields=evidence,
                spoken="The connected provider has not granted the required scope.",
                routed=routed,
                audit={**audit, "missing_provider_scopes": missing_provider},
            )

    if not trusted and not granted_scopes and risk in {"R0", "R1"}:
        return PolicyDecision(
            allowed=False,
            effect="deny",
            reason="verified owner or scoped read access required",
            risk_class=risk,
            required_scopes=scopes,
            confirmation_policy=confirm_policy,
            target=target,
            target_ownership=ownership,
            provider=provider,
            evidence_fields=evidence,
            spoken="That capability needs verified owner access or an explicit scope.",
            routed=routed,
            audit={**audit, "owner_or_scope_required": True},
        )

    independent = risk in {"R3", "R4"}
    fresh_ok = _confirmation_valid(
        confirmation,
        target=target,
        now=clock,
        ttl=ttl,
        risk_class=risk,
        session_id=session_id,
    )

    if risk in {"R3", "R4"} and not fresh_ok:
        spoken = HOLD_LINE
        if target:
            spoken = f"Confirm {_action_phrase(name, target)} on your phone. {HOLD_LINE}"
        return PolicyDecision(
            allowed=False,
            effect="confirm",
            reason="fresh target-bound confirmation required",
            risk_class=risk,
            required_scopes=scopes,
            confirmation_required=True,
            confirmation_policy=CONFIRMATION_FRESH,
            independent_confirmation=True,
            confirmation_ttl_seconds=ttl,
            target=target,
            target_ownership=ownership,
            provider=provider,
            evidence_fields=evidence,
            spoken=spoken,
            routed=routed,
            audit={
                **audit,
                "voice_auth_insufficient": auth_channel == "voice"
                or (confirmation is not None and confirmation.factor in VOICE_FACTORS),
            },
        )

    # For high-risk requests the hold is created before a provider is
    # contacted, so the realtime voice session can remain alive. On approval
    # the same decision is reevaluated and provider absence becomes an honest
    # not_connected/unavailable result instead of a fabricated action.
    if provider_connected is False:
        return PolicyDecision(
            allowed=False,
            effect="not_connected",
            reason=f"provider '{provider or name}' is not connected",
            risk_class=risk,
            required_scopes=scopes,
            confirmation_policy=confirm_policy,
            target=target,
            target_ownership=ownership,
            provider=provider,
            evidence_fields=evidence,
            spoken=f"{_provider_label(provider or name)} is not connected.",
            routed=routed,
            audit=audit,
        )
    if provider_connected is None:
        return PolicyDecision(
            allowed=False,
            effect="unavailable",
            reason=f"provider '{provider or name}' availability is unknown",
            risk_class=risk,
            required_scopes=scopes,
            confirmation_policy=confirm_policy,
            target=target,
            target_ownership=ownership,
            provider=provider,
            evidence_fields=evidence,
            spoken=f"{_provider_label(provider or name)} is unavailable right now.",
            routed=routed,
            audit=audit,
        )

    if (
        risk == "R2"
        and confirm_policy == CONFIRMATION_STANDING
        and not (trusted and (training_wheels_complete or standing_owner_scope))
        and not fresh_ok
    ):
        return PolicyDecision(
            allowed=False,
            effect="confirm",
            reason="R2 requires standing owner scope or confirmation",
            risk_class=risk,
            required_scopes=scopes,
            confirmation_required=True,
            confirmation_policy=CONFIRMATION_STANDING,
            target=target,
            target_ownership=ownership,
            provider=provider,
            evidence_fields=evidence,
            spoken="Confirm that action, or grant a standing scope.",
            routed=routed,
            audit=audit,
        )

    return PolicyDecision(
        allowed=True,
        effect="allow",
        reason="authorized",
        risk_class=risk,
        required_scopes=scopes,
        confirmation_required=False,
        confirmation_policy=confirm_policy,
        independent_confirmation=independent,
        confirmation_ttl_seconds=ttl if independent else None,
        target=target,
        target_ownership=ownership,
        provider=provider,
        evidence_fields=evidence,
        routed=routed,
        audit=audit,
    )


def should_enforce(decision: PolicyDecision, *, name: str, channel: AuthChannel) -> bool:
    """Honor every non-allow decision. Existing gates still run after allow."""

    del name, channel
    return not decision.allowed


def attach_evidence(
    result: dict[str, Any] | None,
    decision: PolicyDecision,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Stamp source/timestamp evidence without fabricating provider success."""

    if not isinstance(result, dict):
        return result
    if result.get("ok") is False or result.get("error") or result.get("degraded"):
        return result
    clock = now or utcnow()
    evidence = dict(result.get("evidence") or {})
    if "source" in decision.evidence_fields and not evidence.get("source"):
        existing = result.get("source")
        if isinstance(existing, dict):
            evidence["source"] = existing.get("kind") or decision.provider or decision.audit.get("name")
        elif existing:
            evidence["source"] = existing
        else:
            evidence["source"] = decision.provider or decision.audit.get("name")
    if "timestamp" in decision.evidence_fields and not evidence.get("timestamp"):
        evidence["timestamp"] = clock.isoformat()
    stamped = dict(result)
    stamped["evidence"] = evidence
    return stamped


def not_connected_payload(decision: PolicyDecision, *, next_step: str | None = None) -> dict[str, Any]:
    payload = decision.to_result()
    payload["error"] = "not_connected"
    payload["degraded"] = True
    if next_step:
        payload["next_step"] = next_step
    elif decision.provider == "calendar":
        payload["next_step"] = (
            "install the calendar integration and grant scope 'calendar:read' "
            "(POST /v1/integrations with adapter=calendar)"
        )
    elif decision.provider == "messaging":
        payload["next_step"] = (
            "install the messaging integration and grant scope 'messaging:read' "
            "(POST /v1/integrations with adapter=messaging)"
        )
    elif decision.provider == "phone":
        payload["next_step"] = (
            "install the phone integration and grant scope 'phone:act' "
            "(POST /v1/integrations with adapter=phone, provider=macos_life or local)"
        )
    elif decision.provider == "mail":
        payload["next_step"] = (
            "install the mail integration and grant scope 'mail:read' "
            "(POST /v1/integrations with adapter=mail)"
        )
    elif decision.provider == "smart_home":
        payload["next_step"] = (
            "install the smart_home integration with provider=local or "
            "provider=homeassistant (POST /v1/integrations with adapter=smart_home)"
        )
    elif decision.provider == "macos_life":
        payload["next_step"] = (
            "install a macos_life messaging bridge and set EV_LIFE_HELPER_PATH "
            "(open-url bridge / apps.activate)"
        )
    elif decision.provider == "search":
        payload["next_step"] = (
            "set EV_SEARCH_PROVIDER=live (Open-Meteo weather, no key) "
            "or EV_SEARCH_PROVIDER=brave with EV_BRAVE_SEARCH_API_KEY"
        )
    return payload


def confirmation_from_request(
    *,
    name: str,
    arguments: dict | None,
    channel: AuthChannel,
    actor: str,
    reverify_token: str | None,
    confirmation: Confirmation | None,
    now: datetime | None = None,
) -> Confirmation | None:
    if confirmation is not None:
        return confirmation
    clock = now or utcnow()
    target = canonical_target(name, arguments)
    if reverify_token and channel == "action":
        return Confirmation(
            factor="reverify",
            confirmed=True,
            target=target,
            issued_at=clock,
            expires_at=clock + timedelta(seconds=R3_TTL_SECONDS),
        )
    args = arguments or {}
    if args.get("confirm"):
        # A body flag is not an independent approval for a device actor. The
        # action endpoint's re-verification proof is the only device path that
        # can produce a fresh factor; HUD/WebAuthn approval is represented by
        # the existing ApprovedAction flow.
        if channel == "action" and actor not in OWNER_ACTORS:
            return None
        factor = "http_approve" if channel == "action" else "voice"
        if actor == "master" and channel == "action":
            factor = "master_key"
        return Confirmation(
            factor=factor,
            confirmed=True,
            target=target,
            issued_at=clock,
            expires_at=clock + timedelta(seconds=R3_TTL_SECONDS),
        )
    if actor == "master" and channel == "action":
        # Master key is an independent factor on the HTTP/action surface, but
        # R3/R4 still need a target-bound confirm flag or reverify.
        return None
    if channel == "voice":
        return Confirmation(factor="voice_wake", confirmed=True, target=target, issued_at=clock)
    return None


async def provider_connected(session: AsyncSession, name: str, spec: dict | None) -> bool | None:
    provider = (spec or {}).get("provider") or PROVIDER_SLUGS.get(name)
    if provider in {None, "local", "open-meteo"}:
        return True
    if provider == "search":
        from app.config import settings

        configured = str(settings.search_provider or "none").strip().lower()
        if configured in {"mock", "live"}:
            return True
        if configured == "brave":
            return bool((settings.brave_search_api_key or "").strip())
        return False
    if provider == "camera":
        camera_id = (
            await session.execute(select(OwnerCamera.id).limit(1))
        ).scalar_one_or_none()
        return camera_id is not None
    if provider == "macos_life":
        from app.ev.apps import find_macos_life_integration

        return await find_macos_life_integration(session) is not None
    if provider not in INTEGRATION_PROVIDERS:
        return True
    row = (
        await session.execute(
            select(Integration).where(
                Integration.adapter == str(provider),
                Integration.status == "active",
            ).limit(1)
        )
    ).scalars().first()
    # Integration presence is the authority-layer connection predicate. The
    # adapter remains responsible for credential/configuration health and must
    # return an honest provider error if the active row is unusable; tests and
    # local doubles deliberately exercise that adapter boundary.
    return row is not None


async def capability_manifest(
    session: AsyncSession,
    *,
    actor: str = "master",
    device_id: UUID | str | None = None,
    realtime_provider: str | None = None,
    channel: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Compatibility entry point for the single runtime projection.

    Policy remains the authority for decisions.  The projection itself lives
    in ``app.ev.capabilities`` so API clients and future model transports use
    exactly the same provider/device/availability view.
    """

    from app.ev.capabilities import build_runtime_projection

    return await build_runtime_projection(
        session,
        actor=actor,
        device_id=device_id,
        realtime_provider=realtime_provider,
        channel=channel,
        session_id=session_id,
    )


async def authorize(
    session: AsyncSession,
    name: str,
    *,
    actor: str,
    arguments: dict | None = None,
    device_id: UUID | None = None,
    channel: str | None = None,
    confirmation: Confirmation | None = None,
    reverify_token: str | None = None,
    spec: dict[str, Any] | None = None,
    granted_scopes: Sequence[str] | None = None,
    session_id: str | None = None,
    provider_scopes_override: Sequence[str] | None = None,
    provider_connected_override: bool | None = None,
    now: datetime | None = None,
) -> PolicyDecision:
    """Gather existing identity/TW/provider state, then evaluate_policy()."""

    from app.ev.delegates import active_for_device, parse_device_id
    from app.ev.training_wheels import remaining_steps

    resolved = spec if spec is not None else resolve_capability(name)
    auth_channel = infer_channel(actor, channel)
    clock = now or utcnow()
    bound_id = None
    device_status: DeviceStatus | None = None
    try:
        bound_id = parse_device_id(device_id)
    except (ValueError, TypeError):
        bound_id = None
        if device_id is not None:
            device_status = "unknown"
    scopes = list(granted_scopes) if granted_scopes is not None else None
    provider_scopes: list[str] | None = None
    device = None
    if actor.startswith("device:") and bound_id is None:
        device_status = "unknown"
    if scopes is None and actor not in OWNER_ACTORS and bound_id is not None:
        row = await active_for_device(session, bound_id)
        if row is not None:
            scopes = list(row.scopes or [])
    if bound_id is not None:
        device = await session.get(Device, bound_id)
        if device is None:
            device_status = "unknown"
        elif device.revoked_at is not None:
            device_status = "revoked"
        elif str(device.trust_level or "") not in {"device", "owner", "master"}:
            device_status = "untrusted"
        elif actor.startswith("device:") and _norm(actor) != _norm(f"device:{device.name}"):
            device_status = "unknown"

    # Provider scopes are part of the existing Integration contract. They stay
    # separate from a delegate's actor scopes, so policy can reject either a
    # caller without authority or a connected provider without its grant.
    provider = (resolved or {}).get("provider") or PROVIDER_SLUGS.get(name)
    if provider_scopes_override is not None:
        provider_scopes = list(provider_scopes_override)
    elif provider in INTEGRATION_PROVIDERS:
        integration = (
            await session.execute(
                select(Integration)
                .where(
                    Integration.adapter == str(provider),
                    Integration.status == "active",
                )
                .order_by(Integration.created_at.asc())
                .limit(1)
            )
        ).scalars().first()
        if integration is not None:
            provider_scopes = list(integration.scopes or [])
    remaining = await remaining_steps(session)
    standing_owner_scope = False
    if actor in OWNER_ACTORS and actor not in MODEL_ACTORS:
        from app.ev.actions import LIFE_ACTION_NAMES, autonomy_mode

        standing_owner_scope = (
            name in LIFE_ACTION_NAMES
            and derive_risk_class(resolved, name) == "R2"
            and autonomy_mode() == "full"
        )
    connected: bool | None = True
    if provider_connected_override is not None:
        connected = provider_connected_override
    elif name in ROUTED_CAPABILITIES:
        connected = await provider_connected(session, name, resolved)
    # Existing routine/worker execution is a scoped authority issued by the
    # stored ApprovedAction/job record. Keep the worker identity in the audit
    # trail, but let the canonical predicate admit its R0/R1 work; R2+ still
    # needs the normal standing/fresh confirmation rules.
    job_trusted = (
        actor in WORKER_ACTORS
        or actor == "automation"
        or actor.startswith("automation:")
        or actor in {"retry:owner", "retry:master"}
        or actor in {"manual:owner", "manual:master"}
    )
    owner_trusted = (actor in OWNER_ACTORS and actor not in MODEL_ACTORS) or job_trusted
    if device is not None and device_status is None and str(device.trust_level or "") == "owner":
        owner_trusted = True
    bound = confirmation_from_request(
        name=name,
        arguments=arguments,
        channel=auth_channel,
        actor=actor,
        reverify_token=reverify_token,
        confirmation=confirmation,
        now=clock,
    )
    return evaluate_policy(
        name,
        spec=resolved,
        actor=actor,
        channel=auth_channel,
        arguments=arguments,
        granted_scopes=scopes,
        provider_scopes=provider_scopes,
        confirmation=bound,
        training_wheels_complete=not remaining,
        provider_connected=connected,
        owner_trusted=owner_trusted,
        device_status=device_status,
        standing_owner_scope=standing_owner_scope,
        session_id=session_id,
        now=clock,
    )


def _provider_label(provider: str) -> str:
    labels = {
        "calendar": "Calendar",
        "messaging": "Messages",
        "open-meteo": "Weather",
        "local": "Diagnostics",
        "phone": "Phone",
        "mail": "Mail",
        "smart_home": "Home Assistant",
        "macos_life": "Mac apps",
        "github": "GitHub",
    }
    return labels.get(provider, provider.replace("_", " ").title())


def _action_phrase(name: str, target: str) -> str:
    if name == "place_call":
        return f"calling {target}"
    if name == "home_act":
        return f"the light change on {target}"
    if name == "calendar_add":
        return f"adding {target} to the calendar"
    if name == "draft_reply":
        return f"sending mail {target}"
    if name == "ticket_buy":
        return f"buying {target}"
    if name == "drone":
        return f"drone command {target}"
    return f"{name} for {target}"
