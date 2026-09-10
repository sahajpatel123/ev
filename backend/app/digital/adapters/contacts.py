"""Google People / Contacts — official People API. Display name is never unique identity."""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.digital.descriptor import ServiceCapabilityDescriptor, cap
from app.digital.fabric import OpContext, OpResult
from app.digital.types import Availability, OpStatus, Verb
from app.digital.vault_bound import diagnose_oauth_error, lease_for, strip_secrets
from app.ev.resolve import pick_unique
from app.integrations import oauth

PEOPLE_API = "https://people.googleapis.com/v1"
CONTACTS_SCOPE = "https://www.googleapis.com/auth/contacts"
CONTACTS_RO = "https://www.googleapis.com/auth/contacts.readonly"


class ContactsPeopleAdapter:
    slug = "contacts"
    display_name = "Google Contacts"

    def descriptors(self) -> list[ServiceCapabilityDescriptor]:
        common = dict(
            credential_type="google_oauth",
            data_classification="contacts",
            backing="people.googleapis.com",
        )
        return [
            cap("contacts", "search", Verb.SEARCH, Availability.NATIVE, read_write="read", risk="R0",
                verification_method="resourceName", **common),
            cap("contacts", "resolve", Verb.RESOLVE, Availability.NATIVE, read_write="read", risk="R0",
                verification_method="unique_or_clarify", **common),
            cap("contacts", "read", Verb.READ, Availability.NATIVE, read_write="read", risk="R0",
                verification_method="resourceName", **common),
            cap("contacts", "create", Verb.CREATE, Availability.NATIVE, read_write="write", risk="R1",
                requires_confirmation=False, verification_method="resourceName", **common),
            cap("contacts", "update", Verb.UPDATE, Availability.NATIVE, read_write="write", risk="R1",
                verification_method="resourceName", **common),
            cap("contacts", "delete", Verb.DELETE, Availability.NATIVE, read_write="write", risk="R2",
                requires_confirmation=True, verification_method="deleted", **common),
            cap("contacts", "list_emails", Verb.LIST, Availability.NATIVE, read_write="read", risk="R0",
                verification_method="emails", **common),
            cap("contacts", "list_phones", Verb.LIST, Availability.NATIVE, read_write="read", risk="R0",
                verification_method="phones", **common),
        ]

    async def execute(self, operation: str, args: dict[str, Any], *, ctx: OpContext) -> OpResult:
        transport = ctx.transport
        lease = None
        if ctx.session is not None:
            lease = await lease_for(ctx.session, "contacts") or await lease_for(ctx.session, "mail")
        if lease is None and transport is None:
            return OpResult(
                status=OpStatus.SERVICE_AUTH_REQUIRED,
                service="contacts",
                operation=operation,
                availability=Availability.CONNECTION_REQUIRED,
                error="contacts_oauth_required",
                diagnosis="oauth_missing",
            )
        try:
            if operation == "resolve":
                return await self._resolve(args, ctx, lease, transport)
            if operation == "search":
                people = await self._search(str(args.get("query") or args.get("name") or ""), lease, transport)
                return OpResult(status=OpStatus.COMPLETED_VERIFIED, service="contacts", operation=operation,
                                availability=Availability.NATIVE,
                                payload={"people": people, "count": len(people)})
            if operation == "read":
                person = await self._get(str(args["resource_name"]), lease, transport)
                return OpResult(status=OpStatus.COMPLETED_VERIFIED, service="contacts", operation=operation,
                                availability=Availability.NATIVE, payload={"person": person})
            if operation == "create":
                person = await self._create(args, lease, transport)
                return OpResult(status=OpStatus.COMPLETED_VERIFIED, service="contacts", operation=operation,
                                availability=Availability.NATIVE,
                                payload={"person": person, "created": True,
                                         "resource_name": person.get("resourceName")},
                                verification={"resource_name": person.get("resourceName")})
            if operation == "update":
                person = await self._update(args, lease, transport)
                return OpResult(status=OpStatus.COMPLETED_VERIFIED, service="contacts", operation=operation,
                                availability=Availability.NATIVE, payload={"person": person})
            if operation == "delete":
                await self._delete(str(args["resource_name"]), lease, transport)
                return OpResult(status=OpStatus.COMPLETED_VERIFIED, service="contacts", operation=operation,
                                availability=Availability.NATIVE, payload={"deleted": True},
                                verification={"deleted": True})
            if operation == "list_emails":
                person = await self._get(str(args["resource_name"]), lease, transport)
                return OpResult(status=OpStatus.COMPLETED_VERIFIED, service="contacts", operation=operation,
                                availability=Availability.NATIVE, payload={"emails": person.get("emails") or []})
            if operation == "list_phones":
                person = await self._get(str(args["resource_name"]), lease, transport)
                return OpResult(status=OpStatus.COMPLETED_VERIFIED, service="contacts", operation=operation,
                                availability=Availability.NATIVE, payload={"phones": person.get("phones") or []})
            return OpResult(status=OpStatus.FAILED, service="contacts", operation=operation,
                            availability=Availability.NATIVE, error="unknown_operation")
        except Exception as exc:
            return OpResult(status=OpStatus.FAILED, service="contacts", operation=operation,
                            availability=Availability.NATIVE, error="people_api_error",
                            diagnosis=diagnose_oauth_error(exc))

    async def _resolve(self, args, ctx, lease, transport) -> OpResult:
        query = str(args.get("query") or args.get("name") or "")
        people = await self._search(query, lease, transport)
        match = pick_unique(
            query,
            people,
            labels=lambda row: [
                str(row.get("name") or ""),
                str(row.get("display_name") or ""),
                *(row.get("emails") or []),
                *(row.get("phones") or []),
                str(row.get("resourceName") or ""),
            ],
        )
        if match.status == "ambiguous":
            return OpResult(
                status=OpStatus.CLARIFY,
                service="contacts",
                operation="resolve",
                availability=Availability.NATIVE,
                error="ambiguous",
                clarify=[
                    {"name": c.get("name"), "resource_name": c.get("resourceName"),
                     "emails": c.get("emails"), "phones": c.get("phones")}
                    for c in match.candidates
                ],
                payload={"sent": False},
            )
        if match.status != "unique" or match.item is None:
            return OpResult(
                status=OpStatus.CLARIFY,
                service="contacts",
                operation="resolve",
                availability=Availability.NATIVE,
                error="not_found",
                payload={"sent": False},
            )
        return OpResult(
            status=OpStatus.COMPLETED_VERIFIED,
            service="contacts",
            operation="resolve",
            availability=Availability.NATIVE,
            payload={"person": match.item, "unique": True},
            verification={"resource_name": match.item.get("resourceName")},
        )

    async def _search(self, query: str, lease, transport) -> list[dict[str, Any]]:
        data = await _people_get(
            "/people:searchContacts",
            params={
                "query": query,
                "readMask": "names,emailAddresses,phoneNumbers,organizations,nicknames,biographies",
                "pageSize": "10",
            },
            lease=lease,
            transport=transport,
        )
        results = data.get("results") if isinstance(data.get("results"), list) else []
        out = []
        for row in results:
            person = row.get("person") if isinstance(row, dict) else None
            if isinstance(person, dict):
                out.append(normalize_person(person))
        return out

    async def _get(self, resource_name: str, lease, transport) -> dict[str, Any]:
        path = resource_name if resource_name.startswith("/") else f"/{resource_name}"
        data = await _people_get(
            path,
            params={"personFields": "names,emailAddresses,phoneNumbers,organizations,nicknames,biographies,metadata"},
            lease=lease,
            transport=transport,
        )
        return normalize_person(data)

    async def _create(self, args: dict[str, Any], lease, transport) -> dict[str, Any]:
        given = str(args.get("given_name") or args.get("givenName") or args.get("name") or "")
        family = str(args.get("family_name") or args.get("familyName") or "")
        name_row: dict[str, Any] = {"givenName": given}
        if family:
            name_row["familyName"] = family
        body: dict[str, Any] = {"names": [name_row]}
        if args.get("email"):
            body["emailAddresses"] = [{"value": str(args["email"])}]
        if args.get("phone"):
            body["phoneNumbers"] = [{"value": str(args["phone"])}]
        if args.get("organization"):
            body["organizations"] = [{"name": str(args["organization"])}]
        data = await _people_post("/people:createContact", json_body=body, lease=lease, transport=transport)
        return normalize_person(data)

    async def _update(self, args: dict[str, Any], lease, transport) -> dict[str, Any]:
        resource = str(args["resource_name"])
        current = await self._get(resource, lease, transport)
        body: dict[str, Any] = {"etag": current.get("etag") or args.get("etag") or ""}
        update_fields = []
        new_name = args.get("name") or args.get("given_name") or args.get("givenName")
        if new_name:
            body["names"] = [{"givenName": str(new_name)}]
            update_fields.append("names")
        if args.get("email"):
            body["emailAddresses"] = [{"value": str(args["email"])}]
            update_fields.append("emailAddresses")
        if args.get("phone"):
            body["phoneNumbers"] = [{"value": str(args["phone"])}]
            update_fields.append("phoneNumbers")
        path = resource if resource.startswith("people/") else resource
        data = await _people_patch(
            f"/{path}:updateContact",
            params={"updatePersonFields": ",".join(update_fields) or "names"},
            json_body=body,
            lease=lease,
            transport=transport,
        )
        return normalize_person(data)

    async def _delete(self, resource_name: str, lease, transport) -> None:
        path = resource_name if resource_name.startswith("/") else f"/{resource_name}"
        await _people_delete(f"{path}:deleteContact", lease=lease, transport=transport)


def normalize_person(data: dict[str, Any]) -> dict[str, Any]:
    names = data.get("names") if isinstance(data.get("names"), list) else []
    display = ""
    if names and isinstance(names[0], dict):
        display = str(names[0].get("displayName") or names[0].get("givenName") or "")
    emails = [
        str(e.get("value"))
        for e in (data.get("emailAddresses") or [])
        if isinstance(e, dict) and e.get("value")
    ]
    phones = [
        str(p.get("value"))
        for p in (data.get("phoneNumbers") or [])
        if isinstance(p, dict) and p.get("value")
    ]
    orgs = data.get("organizations") if isinstance(data.get("organizations"), list) else []
    org = ""
    rel = ""
    if orgs and isinstance(orgs[0], dict):
        org = str(orgs[0].get("name") or "")
        rel = str(orgs[0].get("title") or "")
    return {
        "resourceName": data.get("resourceName"),
        "etag": data.get("etag"),
        "name": display,
        "display_name": display,
        "emails": emails,
        "phones": phones,
        "organization": org,
        "relationship": rel,
        "google_contact_id": data.get("resourceName"),
    }


async def _request(method: str, path: str, *, lease, transport, params=None, json_body=None) -> dict[str, Any]:
    url = path if path.startswith("http") else f"{PEOPLE_API}{path}"
    headers = {"Accept": "application/json"}
    if lease is not None:
        headers.update(lease.header())
    if transport is not None:
        response = await transport.request(method, url, headers=headers, params=params, json_body=json_body)
    else:
        async with oauth.make_http_client(timeout=20.0) as client:
            response = await client.request(method, url, headers=headers, params=params, json=json_body)
    if response.status_code in (401, 403):
        raise oauth.google_api_auth_error(response, "people api")
    if response.status_code >= 400:
        raise oauth.OAuthProviderError(f"people api failed (status {response.status_code})")
    if response.status_code == 204 or not response.content:
        return {}
    data = response.json()
    if not isinstance(data, dict):
        raise oauth.OAuthProviderError("people api malformed JSON")
    return strip_secrets(data)


async def _people_get(path, *, lease, transport, params=None):
    return await _request("GET", path, lease=lease, transport=transport, params=params)


async def _people_post(path, *, lease, transport, json_body=None):
    return await _request("POST", path, lease=lease, transport=transport, json_body=json_body)


async def _people_patch(path, *, lease, transport, params=None, json_body=None):
    return await _request("PATCH", path, lease=lease, transport=transport, params=params, json_body=json_body)


async def _people_delete(path, *, lease, transport):
    return await _request("DELETE", path, lease=lease, transport=transport)


ContactsAdapter = ContactsPeopleAdapter


class FakePeopleTransport:
    """Hermetic People API. Never counts as live REAL PASS."""

    def __init__(self) -> None:
        self.people: dict[str, dict[str, Any]] = {}
        self._n = 0

    async def request(self, method, url, *, headers=None, params=None, json_body=None, content=None):
        del headers, content
        params = params or {}
        if method == "GET" and "people:searchContacts" in url:
            q = str(params.get("query") or "").lower()
            results = []
            for person in self.people.values():
                blob = json.dumps(person).lower()
                if not q or q in blob:
                    results.append({"person": person})
            return httpx.Response(200, json={"results": results})
        if method == "POST" and "people:createContact" in url:
            self._n += 1
            rid = f"people/c{self._n}"
            body = dict(json_body or {})
            body["resourceName"] = rid
            body["etag"] = f"etag-{self._n}"
            names = body.get("names") or []
            if names and isinstance(names[0], dict) and not names[0].get("displayName"):
                names[0]["displayName"] = " ".join(
                    x for x in (names[0].get("givenName"), names[0].get("familyName")) if x
                )
            self.people[rid] = body
            return httpx.Response(200, json=body)
        if method == "PATCH" and ":updateContact" in url:
            rid = url.split("/v1/")[-1].split(":updateContact")[0]
            current = dict(self.people.get(rid) or {"resourceName": rid})
            current.update(json_body or {})
            names = current.get("names") or []
            if names and isinstance(names[0], dict):
                names[0]["displayName"] = " ".join(
                    x for x in (names[0].get("givenName"), names[0].get("familyName")) if x
                )
            self.people[rid] = current
            return httpx.Response(200, json=current)
        if method == "DELETE" and ":deleteContact" in url:
            rid = url.split("/v1/")[-1].split(":deleteContact")[0]
            self.people.pop(rid, None)
            return httpx.Response(204)
        if method == "GET":
            rid = url.split("/v1/")[-1].split("?")[0]
            person = self.people.get(rid) or self.people.get(rid.lstrip("/"))
            if not person:
                return httpx.Response(404, json={"error": {"message": "not found"}})
            return httpx.Response(200, json=person)
        return httpx.Response(200, json={})
