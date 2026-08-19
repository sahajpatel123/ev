"""Voice-bound calls, onboard actuators, and biometric gates for life actions."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ActorContext
from app.ev.actions import autonomy_mode
from app.integrations.life_helper import (
    EXIT_NOT_AVAILABLE,
    LifeHelperError,
    LifeHelperUnavailableError,
)
from app.services.access_log import log_access

WEAPON_RE = re.compile(
    r"\b(instant[_\s-]?kill|kill|weapon|weapons|fire|shoot|detonate|missile|torpedo)\b",
    re.IGNORECASE,
)

SOFTWARE_VERBS = frozenset({"volume.set", "lookout.close", "lookout.open", "hud.card"})
HARDWARE_VERBS = frozenset({"home_act", "drone.cmd"})
ALLOWLIST = SOFTWARE_VERBS | HARDWARE_VERBS

LIFE_BIOMETRIC_TOOLS = frozenset({"place_call", "delegate_grant"})
LIFE_REVERIFY_PURPOSE = "life.action"


def requires_biometric(name: str, args: dict | None = None) -> bool:
    args = args or {}
    if name in LIFE_BIOMETRIC_TOOLS:
        return True
    if name == "home_act":
        action = str(args.get("action") or "").lower()
        return "lock" in action
    if name == "actuate":
        verb = str(args.get("verb") or "").lower()
        nested = args.get("args") if isinstance(args.get("args"), dict) else {}
        if verb == "home_act" and "lock" in str(nested.get("action") or "").lower():
            return True
        if verb == "drone.cmd":
            return True
    return False


def biometric_denied_payload() -> dict:
    return {
        "ok": False,
        "error": "biometric_required",
        "spoken": "I need Face ID, a passkey, or the master key before I do that.",
    }


async def consume_life_reverify(
    session: AsyncSession,
    *,
    actor: str,
    device_id: UUID | None,
    reverify_token: str | None,
    name: str,
    args: dict | None = None,
) -> dict | None:
    """Master key and speaker-verified ears (`actor=voice`) pass.

    Device HTTP actors must consume a purpose-bound reverify proof.
    Ears stay speaker-verify — not Face ID on a headless Mac mic.
    """

    if not requires_biometric(name, args or {}):
        return None
    if actor == "master" or actor == "voice":
        return None
    if not reverify_token:
        return biometric_denied_payload()
    from app.identity.service import IdentityError, consume_reverification

    ctx = ActorContext(
        actor=actor,
        device_id=device_id,
        is_master=False,
    )
    try:
        await consume_reverification(
            session,
            token=reverify_token,
            purpose=LIFE_REVERIFY_PURPOSE,
            ctx=ctx,
        )
    except IdentityError:
        return biometric_denied_payload()
    return None


def _display_name(args: dict, resolved: dict | None) -> str:
    if resolved:
        contact = resolved.get("contact") if isinstance(resolved.get("contact"), dict) else resolved
        if isinstance(contact, dict):
            return str(contact.get("name") or contact.get("display_name") or "").strip()
    for key in ("name", "to", "destination"):
        value = str(args.get(key) or "").strip()
        if value:
            return value
    return "them"


async def _resolve_callee(session: AsyncSession, target: str, *, actor: str) -> dict:
    """Owner address book first, then people-memory display name. Never mix live share."""

    from app.ev import people
    from app.ev.tools import _active_life_integration
    from app.integrations import service as integrations

    resolved: dict[str, Any] = {"query": target, "source": "raw"}
    integration = await _active_life_integration(session, "contacts")
    if integration is not None:
        try:
            outcome = await integrations.execute_action_after_policy(
                session,
                integration.id,
                "contacts.resolve",
                {"name": target, "query": target},
                actor=actor,
            )
            payload = getattr(outcome, "result", None) or {}
            contact = payload.get("contact") if isinstance(payload, dict) else None
            contacts = payload.get("contacts") if isinstance(payload, dict) else None
            if payload.get("error") == "ambiguous" and isinstance(contacts, list):
                return {
                    "query": target,
                    "source": "contacts",
                    "error": "ambiguous",
                    "candidates": contacts,
                    "display_name": target,
                    "destination": target,
                }
            if isinstance(contact, dict) and (contact.get("name") or contact.get("phone") or contact.get("email")):
                resolved = {
                    "query": target,
                    "source": "contacts",
                    "contact": contact,
                    "destination": contact.get("phone") or contact.get("email") or contact.get("destination") or target,
                    "display_name": contact.get("name") or contact.get("display_name") or target,
                }
                return resolved
            if isinstance(contacts, list) and contacts:
                from app.ev.resolve import pick_unique

                match = pick_unique(
                    target,
                    [row for row in contacts if isinstance(row, dict)],
                    labels=lambda row: [
                        str(row.get("name") or ""),
                        str(row.get("display_name") or ""),
                        str(row.get("phone") or ""),
                        str(row.get("email") or ""),
                    ],
                )
                if match.status == "ambiguous":
                    return {
                        "query": target,
                        "source": "contacts",
                        "error": "ambiguous",
                        "candidates": list(match.candidates),
                        "display_name": target,
                        "destination": target,
                    }
                if match.unique and isinstance(match.item, dict):
                    contact = match.item
                    return {
                        "query": target,
                        "source": "contacts",
                        "contact": contact,
                        "destination": contact.get("phone") or contact.get("email") or target,
                        "display_name": contact.get("name") or target,
                    }
        except Exception:
            pass
    try:
        info = await people.whereabouts(session, target)
        dump = info.model_dump() if hasattr(info, "model_dump") else dict(info)
        display = dump.get("name") or target
        resolved = {
            "query": target,
            "source": "people_memory",
            "display_name": display,
            "destination": target,
            "whereabouts": {
                "source_kind": "memory",
                "last_seen": dump.get("last_seen"),
            },
        }
    except Exception:
        resolved = {
            "query": target,
            "source": "raw",
            "display_name": target,
            "destination": target,
        }
    return resolved


async def place_call(
    session: AsyncSession,
    args: dict,
    *,
    actor: str = "master",
) -> dict:
    from app.ev.actuator import (
        CALL_IDEMPOTENCY_TTL,
        DEFAULT_TIMEOUT_SECONDS,
        evidence_base,
        fingerprint,
        prior_result,
        record_actuator,
        with_timeout,
    )
    from app.ev.tools import _active_life_integration, _life_unavailable
    from app.integrations import service as integrations
    from app.utils.text import utcnow

    target = str(args.get("name") or args.get("destination") or args.get("to") or "").strip()
    if not target:
        return {
            "ok": False,
            "error": "missing_destination",
            "spoken": "Who should I call?",
        }
    kind = str(args.get("kind") or ("facetime" if args.get("video") else "tel")).lower()
    if kind not in {"tel", "facetime"}:
        kind = "tel"
    if autonomy_mode() != "full" and not args.get("confirm"):
        return {
            "ok": False,
            "error": "confirm_required",
            "spoken": f"Call {target} on {kind} now?",
        }

    resolved = await _resolve_callee(session, target, actor=actor)
    display = str(resolved.get("display_name") or target)
    destination = str(resolved.get("destination") or target)
    if resolved.get("error") == "ambiguous":
        from app.ev.resolve import ambiguous_spoken, candidate_names

        names = candidate_names(
            resolved.get("candidates") or [],
            name_of=lambda row: str((row or {}).get("name") or (row or {}).get("display_name") or ""),
        )
        return {
            "ok": False,
            "error": "ambiguous",
            "opened": False,
            "candidates": resolved.get("candidates") or [],
            "spoken": ambiguous_spoken("person", names),
        }
    from app.ev.resolve import looks_like_destination

    key = fingerprint("place_call", kind, display, destination)
    replayed = await prior_result(
        session, name="place_call", key=key, max_age=CALL_IDEMPOTENCY_TTL
    )
    if replayed is not None:
        return replayed

    integration = await _active_life_integration(session, "phone")
    if integration is None:
        unavailable = _life_unavailable(
            "no phone bridge is installed",
            next_step=(
                "install the phone integration and grant scope 'phone:act' "
                "(POST /v1/integrations with adapter=phone)"
            ),
        )
        unavailable["spoken"] = "Calling isn't available on this device"
        unavailable["error"] = "not_connected"
        return unavailable
    provider = str((integration.config or {}).get("provider") or "local").lower()
    if (
        not looks_like_destination(destination)
        and not any(ch.isdigit() for ch in destination)
        and provider in {"macos_life", "device_proxy"}
    ):
        return {
            "ok": False,
            "error": "unresolved_destination",
            "opened": False,
            "spoken": f"I don't have a number for {display}.",
            "name": display,
            "destination": destination,
            "resolved": resolved,
        }

    try:
        outcome = await with_timeout(
            integrations.execute_action_after_policy(
                session,
                integration.id,
                "phone.call" if kind == "tel" else "facetime.call",
                {
                    "to": destination,
                    "destination": destination,
                    "kind": kind,
                    "confirm": True,
                },
                actor=actor,
            ),
            seconds=DEFAULT_TIMEOUT_SECONDS,
            spoken="The call request timed out. I will not claim it rang.",
        )
    except LifeHelperUnavailableError:
        return {
            "ok": False,
            "error": "not_available",
            "spoken": "Calling isn't available on this device",
        }
    except LifeHelperError as exc:
        if exc.exit_code == EXIT_NOT_AVAILABLE or exc.error_code in {
            "not_available",
            "unavailable",
        }:
            return {
                "ok": False,
                "error": "not_available",
                "spoken": "Calling isn't available on this device",
            }
        code = exc.error_code or f"exit {exc.exit_code}"
        return {
            "ok": False,
            "error": code,
            "spoken": str(code),
        }
    except Exception as exc:  # noqa: BLE001 - tool boundary
        return {
            "ok": False,
            "error": type(exc).__name__,
            "spoken": str(exc) or type(exc).__name__,
        }

    if isinstance(outcome, dict) and outcome.get("error") in {"timeout", "cancelled"}:
        return outcome

    payload = getattr(outcome, "result", None) or {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    delivery = payload.get("delivery") if isinstance(payload.get("delivery"), dict) else {}
    helper_evidence = delivery.get("evidence") if isinstance(delivery.get("evidence"), dict) else {}
    opened = (
        data.get("opened") is True
        or payload.get("opened") is True
        or helper_evidence.get("opened") is True
    )
    now = utcnow()
    evidence = evidence_base(
        source=str(payload.get("mode") or helper_evidence.get("source") or "phone"),
        accepted=True,
        observed=opened,
        now=now,
        opened=opened,
        destination=destination,
        name=display,
        kind=kind,
        simulated=bool(payload.get("simulated") or payload.get("mode") == "local"),
    )
    if opened:
        spoken = f"Ringing {display}"
        result = {
            "ok": True,
            "spoken": spoken,
            "opened": True,
            "kind": kind,
            "name": display,
            "destination": destination,
            "resolved": resolved,
            "delivery": delivery,
            "evidence": evidence,
        }
        await record_actuator(
            session, name="place_call", actor=actor, key=key, result=result, target=display
        )
        return result
    error = (
        str(data.get("error") or payload.get("error") or helper_evidence.get("error") or "not_opened")
    )
    return {
        "ok": False,
        "opened": False,
        "error": error,
        "spoken": error if error != "not_opened" else f"The call to {display} did not open.",
        "kind": kind,
        "name": display,
        "destination": destination,
        "resolved": resolved,
        "evidence": evidence,
    }


def _weapon_verb(verb: str) -> bool:
    return bool(WEAPON_RE.search(verb or ""))


async def actuate(
    session: AsyncSession,
    verb: str,
    args: dict | None = None,
    *,
    confirm: bool = False,
    actor: str = "master",
) -> dict:
    raw = (verb or "").strip()
    lowered = raw.lower().replace(" ", ".")
    if _weapon_verb(lowered) or _weapon_verb(raw):
        return {
            "ok": False,
            "error": "refused",
            "spoken": "I will not run kill or weapon verbs.",
        }
    if lowered not in ALLOWLIST:
        allow = ", ".join(sorted(ALLOWLIST))
        return {
            "ok": False,
            "error": "unknown_verb",
            "spoken": f"I can only do: {allow}.",
            "allowlist": sorted(ALLOWLIST),
        }
    args = args or {}
    if lowered in HARDWARE_VERBS:
        if autonomy_mode() != "full" and not confirm:
            return {
                "ok": False,
                "error": "confirm_required",
                "spoken": f"Confirm to run {lowered}.",
            }
        await log_access(
            session,
            actor=actor,
            action="actuate",
            endpoint="tool:actuate",
            resource_type="actuator",
            resource_ids=[lowered],
            details={"args": args, "hardware": True},
        )
    if lowered == "volume.set":
        return await _volume_set(session, args)
    if lowered == "lookout.close":
        from app.notify.presence import dismiss_presence

        result = await dismiss_presence(str(args.get("window_id") or "") or None)
        return {"ok": True, "spoken": "Lookout closed.", "result": result}
    if lowered == "lookout.open":
        from app.notify.presence import open_presence

        result = await open_presence(
            title=str(args.get("title") or "Lookout"),
            body=str(args.get("body") or "Lookout open."),
            kind=str(args.get("kind") or "card"),
            lookout=True,
        )
        return {"ok": True, "spoken": "Lookout open.", "result": result}
    if lowered == "hud.card":
        from app.notify.presence import open_presence

        result = await open_presence(
            title=str(args.get("title") or "Card"),
            body=str(args.get("body") or ""),
            kind="card",
        )
        return {"ok": True, "spoken": "Card shown.", "result": result}
    if lowered == "home_act":
        from app.ev.home import home_act

        return await home_act(
            session,
            str(args.get("entity") or args.get("name") or ""),
            str(args.get("action") or ""),
            confirm=confirm or bool(args.get("confirm")),
            actor=actor,
        )
    if lowered == "drone.cmd":
        return {
            "ok": False,
            "error": "training_wheels",
            "spoken": "The drone actuator is locked.",
            "gate": "actuator.drone",
        }
    return {"ok": False, "error": "unknown_verb", "spoken": f"I can only do: {', '.join(sorted(ALLOWLIST))}."}


async def _volume_set(session: AsyncSession, args: dict) -> dict:
    from app.ev.assistant import get_profile

    profile = await get_profile(session)
    current = int(profile.volume_percent or 70)
    if args.get("percent") is not None:
        current = max(0, min(100, int(args["percent"])))
    elif str(args.get("direction") or "").lower() in {"down", "lower", "-"}:
        current = max(0, current - 10)
    elif str(args.get("direction") or "").lower() in {"up", "higher", "+"}:
        current = min(100, current + 10)
    profile.volume_percent = current
    await session.flush()
    return {"ok": True, "spoken": f"Volume {current} percent.", "volume_percent": current}
