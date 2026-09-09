"""Capability Graph projection — Muse answers 'can you?' from live descriptors."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from app.digital.descriptor import ServiceCapabilityDescriptor
from app.digital.fabric import all_descriptors, capability_matrix
from app.digital.types import Availability
from app.digital.vault_bound import lease_for


async def live_descriptors(session=None, *, whatsapp_status: dict[str, Any] | None = None) -> list[ServiceCapabilityDescriptor]:
    """Rewrite availability from actual OAuth/session truth."""
    gmail_ok = False
    gmail_write = False
    contacts_ok = False
    calendar_ok = False
    calendar_write = False
    if session is not None:
        mail = await lease_for(session, "mail") or await lease_for(session, "gmail_ops")
        if mail:
            gmail_ok = True
            scopes = set(mail.scopes)
            gmail_write = bool(
                scopes
                & {
                    "https://www.googleapis.com/auth/gmail.send",
                    "https://www.googleapis.com/auth/gmail.modify",
                    "https://www.googleapis.com/auth/gmail.compose",
                    "mail:act",
                }
            )
        contacts = await lease_for(session, "contacts")
        contacts_ok = contacts is not None
        cal = await lease_for(session, "calendar")
        if cal:
            calendar_ok = True
            calendar_write = bool(
                set(cal.scopes)
                & {
                    "https://www.googleapis.com/auth/calendar",
                    "https://www.googleapis.com/auth/calendar.events",
                    "calendar:act",
                    "calendar:write",
                }
            )
    wa_auth = bool((whatsapp_status or {}).get("authenticated"))
    out: list[ServiceCapabilityDescriptor] = []
    for d in all_descriptors():
        av = d.availability
        if d.service == "gmail":
            if not gmail_ok or d.read_write == "write" and not gmail_write:
                av = Availability.CONNECTION_REQUIRED
            else:
                av = Availability.NATIVE
        elif d.service == "contacts":
            av = Availability.NATIVE if contacts_ok or gmail_ok else Availability.CONNECTION_REQUIRED
        elif d.service == "calendar":
            if not calendar_ok or d.read_write == "write" and not calendar_write:
                av = Availability.CONNECTION_REQUIRED
            else:
                av = Availability.NATIVE
        elif d.service == "whatsapp":
            av = Availability.OPERATED if wa_auth else Availability.CONNECTION_REQUIRED
        out.append(replace(d, availability=av))
    return out


def matrix_from(descs: list[ServiceCapabilityDescriptor]) -> dict[str, dict[str, str]]:
    matrix: dict[str, dict[str, str]] = {}
    for d in descs:
        matrix.setdefault(d.service, {})[d.operation] = d.availability.value
    return matrix


def semantic_digital_families(descs: list[ServiceCapabilityDescriptor] | None = None) -> dict[str, dict[str, Any]]:
    descs = descs or all_descriptors()
    families: dict[str, dict[str, Any]] = {}
    for d in descs:
        key = f"{d.service.upper()}_{d.verb.value}"
        families[key] = {
            "registered": True,
            "controller_bound": True,
            "operations": [f"{d.service}.{d.operation}"],
            "policy_allowed": d.availability not in {Availability.UNAVAILABLE},
            "runtime_available": d.can_now(),
            "realtime_direct_tool": False,
            "execution_owner": "DigitalOperations",
            "availability": d.availability.value,
            "source_derived": True,
        }
    return families


def static_matrix() -> dict[str, dict[str, str]]:
    return capability_matrix()
