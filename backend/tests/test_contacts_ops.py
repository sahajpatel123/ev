"""Contact writes are verified by the person Google returns, not by the request."""

from __future__ import annotations

from app.digital.adapters.contacts import ContactsPeopleAdapter
from app.digital.fabric import OpContext
from app.digital.types import OpStatus


class _Response:
    def __init__(self, status_code: int, body: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body or {}
        self.content = b"" if status_code == 204 else b"{}"

    def json(self) -> dict:
        return self._body


class _Transport:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    async def request(self, method, url, headers=None, params=None, json_body=None):
        del headers, params, json_body
        self.calls.append(method)
        return self.responses.pop(0)


def _ctx(transport: _Transport) -> OpContext:
    return OpContext(actor="owner", transport=transport, confirmed=True)


async def test_contact_create_without_a_resource_name_is_not_verified() -> None:
    transport = _Transport([_Response(200, {"names": [{"givenName": "Ada"}]})])
    result = await ContactsPeopleAdapter().execute(
        "create", {"name": "Ada"}, ctx=_ctx(transport)
    )
    assert result.status == OpStatus.FAILED
    assert result.error == "resource_name_missing"
    assert result.payload["created"] is False


async def test_contact_create_keeps_the_returned_resource_name() -> None:
    transport = _Transport([
        _Response(200, {
            "resourceName": "people/ada",
            "names": [{"givenName": "Ada", "displayName": "Ada"}],
        }),
    ])
    result = await ContactsPeopleAdapter().execute(
        "create", {"name": "Ada"}, ctx=_ctx(transport)
    )
    assert result.status == OpStatus.COMPLETED_VERIFIED
    assert result.verification == {"resource_name": "people/ada"}
    assert result.payload["created"] is True


async def test_contact_delete_is_verified_only_after_the_person_is_gone() -> None:
    still_there = _Transport([
        _Response(204),
        _Response(200, {"resourceName": "people/ada", "names": [{"displayName": "Ada"}]}),
    ])
    kept = await ContactsPeopleAdapter().execute(
        "delete", {"resource_name": "people/ada"}, ctx=_ctx(still_there)
    )
    assert kept.status == OpStatus.FAILED
    assert kept.error == "contact_still_present"
    assert kept.payload["deleted"] is False

    gone = _Transport([_Response(204), _Response(404)])
    removed = await ContactsPeopleAdapter().execute(
        "delete", {"resource_name": "people/ada"}, ctx=_ctx(gone)
    )
    assert removed.status == OpStatus.COMPLETED_VERIFIED
    assert removed.payload["deleted"] is True
    assert removed.verification == {"deleted": True}
    assert gone.calls == ["DELETE", "GET"]
