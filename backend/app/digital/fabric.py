"""DigitalServiceAdapter protocol, registry, and policy-gated execute."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.digital.descriptor import ServiceCapabilityDescriptor
from app.digital.taint import taint_external
from app.digital.types import (
    MASS_ACTION_MAX,
    AutonomyLevel,
    Availability,
    OpStatus,
    Verb,
)
from app.digital.vault_bound import strip_secrets


class DigitalServiceAdapter(Protocol):
    slug: str
    display_name: str

    def descriptors(self) -> list[ServiceCapabilityDescriptor]:
        ...

    async def execute(self, operation: str, args: dict[str, Any], *, ctx: OpContext) -> OpResult:
        ...


@dataclass
class OpContext:
    actor: str = "owner"
    autonomy: AutonomyLevel = AutonomyLevel.SEND_WITH_CONFIRMATION
    confirmed: bool = False
    goal_id: str | None = None
    session: Any = None  # AsyncSession | None
    idempotency_key: str | None = None
    max_items: int = MASS_ACTION_MAX
    transport: Any = None  # test Gmail/People HTTP seam; never a credential
    whatsapp_backing: Any = None
    lease: Any = None  # TokenLease for hermetic adapter tests; never logged


@dataclass
class OpResult:
    status: OpStatus
    service: str
    operation: str
    availability: Availability
    payload: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    diagnosis: str | None = None
    verification: dict[str, Any] | None = None
    taint: dict[str, Any] | None = None
    clarify: list[dict[str, Any]] | None = None

    def as_model(self) -> dict[str, Any]:
        """Muse-visible view: no credentials, external content tagged.

        ``ok`` is a machine-consistent success flag derived from the status,
        not from the payload. It is True only when the status honestly
        completes or prepares the operation (COMPLETED_VERIFIED, PREPARED);
        every pending, blocked, or failed status yields False. ``ok`` never
        fabricates success and is always present in the model.
        """
        body = strip_secrets(dict(self.payload))
        out = {
            "ok": self.status in _OK_STATUSES,
            "status": self.status.value,
            "service": self.service,
            "operation": self.operation,
            "availability": self.availability.value,
            "error": self.error,
            "diagnosis": self.diagnosis,
            "verification": strip_secrets(self.verification) if self.verification else None,
            "clarify": self.clarify,
            "payload": body,
        }
        if self.taint:
            out["origin"] = self.taint.get("origin")
            out["authority"] = self.taint.get("authority")
            out["potential_external_instruction"] = self.taint.get(
                "potential_external_instruction"
            )
        return out



# Statuses that honestly completed or prepared the operation. Everything
# else (waiting, blocked, failed, unknown, clarify) is not success.
_OK_STATUSES = frozenset({OpStatus.COMPLETED_VERIFIED, OpStatus.PREPARED})

_REGISTRY: dict[str, DigitalServiceAdapter] = {}


def register(adapter: DigitalServiceAdapter) -> DigitalServiceAdapter:
    _REGISTRY[adapter.slug] = adapter
    return adapter


def get_adapter(slug: str) -> DigitalServiceAdapter | None:
    ensure_builtin()
    return _REGISTRY.get(slug)


def all_adapters() -> list[DigitalServiceAdapter]:
    ensure_builtin()
    return list(_REGISTRY.values())


def all_descriptors() -> list[ServiceCapabilityDescriptor]:
    out: list[ServiceCapabilityDescriptor] = []
    for adapter in all_adapters():
        out.extend(adapter.descriptors())
    return out


def descriptor_for(service: str, operation: str) -> ServiceCapabilityDescriptor | None:
    for d in all_descriptors():
        if d.service == service and d.operation == operation:
            return d
    return None


def capability_matrix() -> dict[str, dict[str, str]]:
    """Runtime truth table: Gmail.search → NATIVE, WhatsApp.send → OPERATED, …"""
    matrix: dict[str, dict[str, str]] = {}
    for d in all_descriptors():
        matrix.setdefault(d.service, {})[d.operation] = d.availability.value
    return matrix


def answer_can_you(question: str) -> dict[str, Any]:
    """Owner: 'What can you currently do with WhatsApp?' — from live descriptors."""
    q = (question or "").lower()
    service = None
    for slug in ("gmail", "whatsapp", "contacts", "calendar", "browser", "files", "phone"):
        if slug in q:
            service = slug
            break
    if "phone" in q or "iphone" in q:
        service = service or "phone"
    descs = [d for d in all_descriptors() if service is None or d.service == service]
    can = [
        f"{d.service}.{d.operation} ({d.availability.value})"
        for d in descs
        if d.availability not in {Availability.UNAVAILABLE, Availability.CONNECTION_REQUIRED}
    ]
    cannot = [
        f"{d.service}.{d.operation} ({d.availability.value})"
        for d in descs
        if d.availability in {Availability.UNAVAILABLE, Availability.CONNECTION_REQUIRED}
    ]
    spoken_bits = can[:12] or ["nothing confirmed on that service yet"]
    return {
        "spoken": "Currently: " + "; ".join(spoken_bits) + ".",
        "can": can,
        "cannot": cannot,
        "service": service,
        "authority": "digital.capability_graph",
    }


async def execute(
    service: str,
    operation: str,
    args: dict[str, Any] | None = None,
    *,
    ctx: OpContext | None = None,
) -> OpResult:
    ctx = ctx or OpContext()
    args = dict(args or {})
    adapter = get_adapter(service)
    spec = descriptor_for(service, operation)
    if adapter is None or spec is None:
        return OpResult(
            status=OpStatus.FAILED,
            service=service,
            operation=operation,
            availability=Availability.UNAVAILABLE,
            error="unknown_capability",
            diagnosis="capability_missing",
        )
    if spec.availability == Availability.UNAVAILABLE:
        return OpResult(
            status=OpStatus.FAILED,
            service=service,
            operation=operation,
            availability=spec.availability,
            error="unavailable",
            diagnosis="platform_boundary",
        )
    if spec.availability == Availability.CONNECTION_REQUIRED:
        return OpResult(
            status=OpStatus.SERVICE_AUTH_REQUIRED,
            service=service,
            operation=operation,
            availability=spec.availability,
            error="connection_required",
            diagnosis="oauth_or_session_missing",
        )
    selection = args.get("ids") or args.get("message_ids") or args.get("targets")
    if isinstance(selection, list) and len(selection) > ctx.max_items:
        return OpResult(
            status=OpStatus.BLOCKED,
            service=service,
            operation=operation,
            availability=spec.availability,
            error="mass_action_cap",
            diagnosis=f"selection {len(selection)} exceeds max {ctx.max_items}",
        )
    write = spec.read_write == "write"
    sendish = spec.verb in {Verb.SEND, Verb.DELETE} or operation in {
        "send",
        "reply",
        "reply_all",
        "forward",
        "delete",
        "trash",
    }
    if write and sendish:
        if ctx.autonomy == AutonomyLevel.READ:
            return OpResult(
                status=OpStatus.BLOCKED,
                service=service,
                operation=operation,
                availability=spec.availability,
                error="autonomy_read_only",
                diagnosis="READ autonomy forbids external mutation",
            )
        if ctx.autonomy == AutonomyLevel.PREPARE_ONLY and spec.verb == Verb.SEND:
            return OpResult(
                status=OpStatus.PREPARED,
                service=service,
                operation="prepare",
                availability=spec.availability,
                payload={"prepared": True, "sent": False, "reason": "PREPARE_ONLY"},
            )
        if (
            ctx.autonomy in {AutonomyLevel.SEND_WITH_CONFIRMATION, AutonomyLevel.PREPARE_ONLY}
            and spec.requires_confirmation
            and not ctx.confirmed
            and spec.verb == Verb.SEND
        ):
            return OpResult(
                status=OpStatus.WAITING_FOR_APPROVAL,
                service=service,
                operation=operation,
                availability=spec.availability,
                payload={"prepared": True, "sent": False},
                error="confirmation_required",
            )
    try:
        result = await adapter.execute(operation, args, ctx=ctx)
    except Exception as exc:
        from app.digital.vault_bound import diagnose_oauth_error

        return OpResult(
            status=OpStatus.FAILED,
            service=service,
            operation=operation,
            availability=spec.availability,
            error="adapter_error",
            diagnosis=diagnose_oauth_error(exc),
        )
    result.payload = strip_secrets(result.payload)
    if spec.read_write == "read" and result.payload and result.taint is None:
        result.taint = taint_external(
            result.payload, source=service, extra={"operation": operation}
        )
    if write and ctx.session is not None:
        from app.digital.audit import record_op

        target = str(args.get("to") or args.get("chat_ref") or args.get("message_id") or args.get("resource_name") or "")
        await record_op(
            ctx.session,
            goal_id=ctx.goal_id,
            service=service,
            operation=operation,
            target=target or None,
            risk=spec.risk,
            result=result.status.value,
            verification=result.verification,
        )
    return result


_BOOTED = False


def ensure_builtin() -> None:
    global _BOOTED
    if _BOOTED:
        return
    _BOOTED = True
    from app.digital.adapters import browser, calendar, contacts, files, gmail, phone, whatsapp

    register(gmail.GmailAdapter())
    register(contacts.ContactsPeopleAdapter())
    register(calendar.CalendarOpsAdapter())
    register(whatsapp.WhatsAppWebAdapter())
    register(browser.BrowserOperationsAdapter())
    register(files.FileServiceAdapter())
    register(phone.PhoneDigitalAdapter())
