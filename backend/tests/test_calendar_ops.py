"""Calendar writes are verified by the event Google returns, not by the request."""

from __future__ import annotations

from app.digital.adapters.calendar import CalendarOpsAdapter
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
        self.calls: list[tuple[str, str]] = []

    async def request(self, method, url, headers=None, params=None, json_body=None):
        del headers, params, json_body
        self.calls.append((method, url))
        return self.responses.pop(0)


def _ctx(transport: _Transport) -> OpContext:
    return OpContext(actor="owner", transport=transport, confirmed=True)


async def test_calendar_create_without_an_id_is_not_verified() -> None:
    transport = _Transport([_Response(200, {"summary": "Lunch"})])
    result = await CalendarOpsAdapter().execute(
        "create",
        {"summary": "Lunch", "start": "2026-10-20T09:00:00Z", "end": "2026-10-20T10:00:00Z"},
        ctx=_ctx(transport),
    )
    assert result.status == OpStatus.FAILED
    assert result.error == "event_id_missing"
    assert result.verification is None


async def test_calendar_create_keeps_the_returned_event_id() -> None:
    transport = _Transport([
        _Response(200, {
            "id": "evt-1",
            "summary": "Lunch",
            "start": {"dateTime": "2026-10-20T09:00:00Z"},
            "end": {"dateTime": "2026-10-20T10:00:00Z"},
        }),
    ])
    result = await CalendarOpsAdapter().execute(
        "create",
        {"summary": "Lunch", "start": "2026-10-20T09:00:00Z", "end": "2026-10-20T10:00:00Z"},
        ctx=_ctx(transport),
    )
    assert result.status == OpStatus.COMPLETED_VERIFIED
    assert result.verification == {"event_id": "evt-1"}


async def test_calendar_delete_is_verified_only_after_the_event_is_gone() -> None:
    still_there = _Transport([
        _Response(204),
        _Response(200, {"id": "evt-1", "status": "confirmed", "summary": "Lunch"}),
    ])
    kept = await CalendarOpsAdapter().execute(
        "delete", {"event_id": "evt-1"}, ctx=_ctx(still_there)
    )
    assert kept.status == OpStatus.FAILED
    assert kept.error == "event_still_present"
    assert kept.payload["cancelled"] is False

    gone = _Transport([_Response(204), _Response(404, {})])
    removed = await CalendarOpsAdapter().execute(
        "delete", {"event_id": "evt-1"}, ctx=_ctx(gone)
    )
    assert removed.status == OpStatus.COMPLETED_VERIFIED
    assert removed.payload["cancelled"] is True
    assert removed.verification == {"cancelled": True}
    assert [method for method, _url in gone.calls] == ["DELETE", "GET"]
