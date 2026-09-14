"""Calendar operations — reuse Google Calendar authority; do not duplicate truths."""

from __future__ import annotations

from typing import Any

from app.digital.descriptor import ServiceCapabilityDescriptor, cap
from app.digital.fabric import OpContext, OpResult
from app.digital.types import Availability, OpStatus, Verb
from app.digital.vault_bound import diagnose_oauth_error, lease_for, strip_secrets
from app.integrations import oauth

CAL_API = "https://www.googleapis.com/calendar/v3"
CAL_RO = "https://www.googleapis.com/auth/calendar.readonly"
CAL_EVENTS = "https://www.googleapis.com/auth/calendar.events"
CAL_FULL = "https://www.googleapis.com/auth/calendar"


class CalendarOpsAdapter:
    slug = "calendar"
    display_name = "Google Calendar"

    def descriptors(self) -> list[ServiceCapabilityDescriptor]:
        common = dict(
            credential_type="google_oauth",
            data_classification="calendar",
            backing="googleapis.com/calendar/v3",
        )
        return [
            cap("calendar", "list", Verb.LIST, Availability.NATIVE, read_write="read", risk="R0",
                verification_method="event_ids", **common),
            cap("calendar", "search", Verb.SEARCH, Availability.NATIVE, read_write="read", risk="R0",
                verification_method="event_ids", **common),
            cap("calendar", "read", Verb.READ, Availability.NATIVE, read_write="read", risk="R0",
                verification_method="event_id", **common),
            cap("calendar", "create", Verb.CREATE, Availability.NATIVE, read_write="write", risk="R2",
                requires_confirmation=True, verification_method="event_id", **common),
            cap("calendar", "update", Verb.UPDATE, Availability.NATIVE, read_write="write", risk="R2",
                requires_confirmation=True, verification_method="event_id", **common),
            cap("calendar", "cancel", Verb.DELETE, Availability.NATIVE, read_write="write", risk="R2",
                requires_confirmation=True, verification_method="cancelled", **common),
            cap("calendar", "delete", Verb.DELETE, Availability.NATIVE, read_write="write", risk="R2",
                requires_confirmation=True, verification_method="cancelled", **common),
            cap("calendar", "attendees", Verb.READ, Availability.NATIVE, read_write="read", risk="R0",
                verification_method="emails", **common),
            cap("calendar", "availability", Verb.READ, Availability.NATIVE, read_write="read", risk="R0",
                verification_method="busy", **common),
        ]

    async def execute(self, operation: str, args: dict[str, Any], *, ctx: OpContext) -> OpResult:
        lease = None
        if ctx.session is not None:
            lease = await lease_for(ctx.session, "calendar")
        transport = ctx.transport
        if lease is None and transport is None:
            return OpResult(
                status=OpStatus.SERVICE_AUTH_REQUIRED,
                service="calendar",
                operation=operation,
                availability=Availability.CONNECTION_REQUIRED,
                error="calendar_oauth_required",
                diagnosis="oauth_missing",
            )
        try:
            if operation in {"list", "search"}:
                items = await _list_events(args, lease, transport)
                return OpResult(status=OpStatus.COMPLETED_VERIFIED, service="calendar", operation=operation,
                                availability=Availability.NATIVE, payload={"events": items})
            if operation == "read":
                item = await _get_event(str(args.get("calendar_id") or "primary"), str(args["event_id"]), lease, transport)
                return OpResult(status=OpStatus.COMPLETED_VERIFIED, service="calendar", operation=operation,
                                availability=Availability.NATIVE, payload={"event": item})
            if operation == "attendees":
                item = await _get_event(str(args.get("calendar_id") or "primary"), str(args["event_id"]), lease, transport)
                return OpResult(status=OpStatus.COMPLETED_VERIFIED, service="calendar", operation=operation,
                                availability=Availability.NATIVE,
                                payload={"attendees": item.get("attendees") or []})
            if operation == "availability":
                busy = await _freebusy(args, lease, transport)
                return OpResult(status=OpStatus.COMPLETED_VERIFIED, service="calendar", operation=operation,
                                availability=Availability.NATIVE, payload={"busy": busy})
            if operation == "create":
                item = await _insert_event(args, lease, transport)
                return OpResult(status=OpStatus.COMPLETED_VERIFIED, service="calendar", operation=operation,
                                availability=Availability.NATIVE, payload={"event": item},
                                verification={"event_id": item.get("id")})
            if operation == "update":
                item = await _patch_event(args, lease, transport)
                return OpResult(status=OpStatus.COMPLETED_VERIFIED, service="calendar", operation=operation,
                                availability=Availability.NATIVE, payload={"event": item})
            if operation in {"cancel", "delete"}:
                await _delete_event(str(args.get("calendar_id") or "primary"), str(args["event_id"]), lease, transport)
                return OpResult(status=OpStatus.COMPLETED_VERIFIED, service="calendar", operation=operation,
                                availability=Availability.NATIVE, payload={"cancelled": True},
                                verification={"cancelled": True})
            return OpResult(status=OpStatus.FAILED, service="calendar", operation=operation,
                            availability=Availability.NATIVE, error="unknown_operation")
        except Exception as exc:
            return OpResult(status=OpStatus.FAILED, service="calendar", operation=operation,
                            availability=Availability.NATIVE, error="calendar_api_error",
                            diagnosis=diagnose_oauth_error(exc))


def normalize_event(item: dict[str, Any]) -> dict[str, Any]:
    attendees = []
    for a in item.get("attendees") or []:
        if isinstance(a, dict):
            attendees.append(
                {
                    "email": a.get("email"),
                    "display_name": a.get("displayName"),
                    "response": a.get("responseStatus"),
                    "self": bool(a.get("self")),
                }
            )
    start = item.get("start") if isinstance(item.get("start"), dict) else {}
    end = item.get("end") if isinstance(item.get("end"), dict) else {}
    return {
        "id": item.get("id"),
        "calendar_id": item.get("organizer", {}).get("email") if isinstance(item.get("organizer"), dict) else None,
        "summary": item.get("summary"),
        "description": (item.get("description") or "")[:2000],
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "attendees": attendees,
        "html_link": item.get("htmlLink"),
        "status": item.get("status"),
        "origin": "EXTERNAL_CONTENT",
        "authority": "DATA",
    }


async def _call(method, path, *, lease, transport, params=None, json_body=None) -> dict[str, Any]:
    url = path if path.startswith("http") else f"{CAL_API}{path}"
    headers = {"Accept": "application/json"}
    if lease is not None:
        headers.update(lease.header())
    if transport is not None:
        response = await transport.request(method, url, headers=headers, params=params, json_body=json_body)
    else:
        async with oauth.make_http_client(timeout=20.0) as client:
            response = await client.request(method, url, headers=headers, params=params, json=json_body)
    if response.status_code in (401, 403):
        raise oauth.google_api_auth_error(response, "calendar")
    if response.status_code >= 400:
        raise oauth.OAuthProviderError(f"calendar failed (status {response.status_code})")
    if response.status_code == 204 or not getattr(response, "content", None):
        return {}
    data = response.json()
    return strip_secrets(data) if isinstance(data, dict) else {}


async def _list_events(args, lease, transport) -> list[dict[str, Any]]:
    params = {
        "maxResults": str(min(int(args.get("limit") or 20), 50)),
        "singleEvents": "true",
        "orderBy": "startTime",
    }
    if args.get("time_min"):
        params["timeMin"] = str(args["time_min"])
    if args.get("time_max"):
        params["timeMax"] = str(args["time_max"])
    if args.get("q") or args.get("query"):
        params["q"] = str(args.get("q") or args.get("query"))
    cal = str(args.get("calendar_id") or "primary")
    data = await _call("GET", f"/calendars/{cal}/events", lease=lease, transport=transport, params=params)
    items = data.get("items") if isinstance(data.get("items"), list) else []
    return [normalize_event(i) for i in items if isinstance(i, dict)]


async def _get_event(calendar_id, event_id, lease, transport) -> dict[str, Any]:
    data = await _call("GET", f"/calendars/{calendar_id}/events/{event_id}", lease=lease, transport=transport)
    return normalize_event(data)


async def _insert_event(args, lease, transport) -> dict[str, Any]:
    cal = str(args.get("calendar_id") or "primary")
    body = {
        "summary": str(args.get("summary") or args.get("title") or ""),
        "start": {"dateTime": str(args["start"])},
        "end": {"dateTime": str(args["end"])},
    }
    if args.get("description"):
        body["description"] = str(args["description"])[:4000]
    if args.get("attendees"):
        body["attendees"] = [{"email": e} for e in args["attendees"]]
    data = await _call("POST", f"/calendars/{cal}/events", lease=lease, transport=transport, json_body=body)
    return normalize_event(data)


async def _patch_event(args, lease, transport) -> dict[str, Any]:
    cal = str(args.get("calendar_id") or "primary")
    eid = str(args["event_id"])
    body: dict[str, Any] = {}
    if args.get("summary") or args.get("title"):
        body["summary"] = str(args.get("summary") or args.get("title"))
    if args.get("start"):
        body["start"] = {"dateTime": str(args["start"])}
    if args.get("end"):
        body["end"] = {"dateTime": str(args["end"])}
    data = await _call("PATCH", f"/calendars/{cal}/events/{eid}", lease=lease, transport=transport, json_body=body)
    return normalize_event(data)


async def _delete_event(calendar_id, event_id, lease, transport) -> None:
    await _call("DELETE", f"/calendars/{calendar_id}/events/{event_id}", lease=lease, transport=transport)


async def _freebusy(args, lease, transport) -> list[dict[str, Any]]:
    body = {
        "timeMin": str(args.get("time_min") or args.get("start")),
        "timeMax": str(args.get("time_max") or args.get("end")),
        "items": [{"id": str(args.get("calendar_id") or "primary")}],
    }
    data = await _call("POST", "/freeBusy", lease=lease, transport=transport, json_body=body)
    cals = data.get("calendars") if isinstance(data.get("calendars"), dict) else {}
    busy = []
    for _id, block in cals.items():
        if isinstance(block, dict):
            for row in block.get("busy") or []:
                if isinstance(row, dict):
                    busy.append({"start": row.get("start"), "end": row.get("end")})
    return busy
