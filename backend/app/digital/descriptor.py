"""Service capability descriptors — Capability Router / Muse projection source."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from app.digital.types import Availability, Verb


@dataclass(frozen=True)
class ServiceCapabilityDescriptor:
    service: str
    operation: str
    verb: Verb
    availability: Availability
    read_write: str  # "read" | "write"
    risk: str  # R0-R4
    requires_confirmation: bool
    requires_foreground: bool
    requires_owner_presence: bool
    supports_background: bool
    verification_method: str
    credential_type: str
    data_classification: str
    rate_limit: str
    backing: str  # provider/API/UI
    scopes: tuple[str, ...] = ()
    notes: str = ""

    def as_public(self) -> dict[str, Any]:
        """Model-facing projection: no secrets, no selectors, no tokens."""
        d = asdict(self)
        d["verb"] = self.verb.value
        d["availability"] = self.availability.value
        d.pop("scopes", None)  # OAuth scope strings stay in the adapter, not Muse
        return d

    def can_now(self) -> bool:
        return self.availability in {Availability.NATIVE, Availability.OPERATED, Availability.PREPARED}


def cap(
    service: str,
    operation: str,
    verb: Verb,
    availability: Availability,
    *,
    read_write: str,
    risk: str,
    requires_confirmation: bool = False,
    requires_foreground: bool = False,
    requires_owner_presence: bool = False,
    supports_background: bool = True,
    verification_method: str,
    credential_type: str,
    data_classification: str = "external",
    rate_limit: str = "provider",
    backing: str,
    scopes: tuple[str, ...] = (),
    notes: str = "",
) -> ServiceCapabilityDescriptor:
    return ServiceCapabilityDescriptor(
        service=service,
        operation=operation,
        verb=verb,
        availability=availability,
        read_write=read_write,
        risk=risk,
        requires_confirmation=requires_confirmation,
        requires_foreground=requires_foreground,
        requires_owner_presence=requires_owner_presence,
        supports_background=supports_background,
        verification_method=verification_method,
        credential_type=credential_type,
        data_classification=data_classification,
        rate_limit=rate_limit,
        backing=backing,
        scopes=scopes,
        notes=notes,
    )
