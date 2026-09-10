"""Phone role in Digital Operations: interaction surface, not Gmail/WhatsApp executor."""

from __future__ import annotations

from typing import Any

from app.digital.descriptor import ServiceCapabilityDescriptor, cap
from app.digital.fabric import OpContext, OpResult
from app.digital.types import Availability, OpStatus, Verb


class PhoneDigitalAdapter:
    slug = "phone"
    display_name = "iPhone Pocket / Satellite"

    def descriptors(self) -> list[ServiceCapabilityDescriptor]:
        common = dict(
            credential_type="device_trust",
            data_classification="owner_device",
            backing="PWA / Tailscale / Device Gateway",
        )
        return [
            cap("phone", "ask", Verb.READ, Availability.NATIVE, read_write="read", risk="R0",
                verification_method="turn", **common),
            cap("phone", "approve", Verb.EXECUTE, Availability.NATIVE, read_write="write", risk="R2",
                requires_owner_presence=True, verification_method="ApprovedAction", **common),
            cap("phone", "call", Verb.PREPARE, Availability.OS_MEDIATED, read_write="write", risk="R3",
                requires_foreground=True, requires_owner_presence=True,
                verification_method="handoff", **common,
                notes="OS-mediated; no connection evidence claimed"),
            cap("phone", "capture_share", Verb.UPLOAD, Availability.NATIVE, read_write="write", risk="R1",
                verification_method="artifact", **common),
            cap("phone", "physical_action", Verb.EXECUTE, Availability.UNAVAILABLE, read_write="write", risk="R4",
                verification_method="none", **common, notes="No new hardware"),
        ]

    async def execute(self, operation: str, args: dict[str, Any], *, ctx: OpContext) -> OpResult:
        if operation == "physical_action":
            return OpResult(
                status=OpStatus.FAILED,
                service="phone",
                operation=operation,
                availability=Availability.UNAVAILABLE,
                error="unavailable",
                diagnosis="platform_boundary",
            )
        if operation == "call":
            return OpResult(
                status=OpStatus.PREPARED,
                service="phone",
                operation=operation,
                availability=Availability.OS_MEDIATED,
                payload={"prepared": True, "connected": False, "note": "OS-mediated handoff only"},
            )
        if operation in {"ask", "approve", "capture_share"}:
            # Honest refusal: no device bridge is wired for these operations
            # yet. Never fabricate success for an operation not performed.
            return OpResult(
                status=OpStatus.FAILED,
                service="phone",
                operation=operation,
                availability=Availability.NATIVE,
                error="phone_channel_unavailable",
                diagnosis=f"no device bridge wired for phone.{operation}",
                payload={"refused": True, "performed": False, "operation": operation},
            )
        return OpResult(status=OpStatus.FAILED, service="phone", operation=operation,
                        availability=Availability.NATIVE, error="unknown_operation")
