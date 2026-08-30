"""Gateway-to-provider calendar write coverage (vault, evidence, timeout)."""

from __future__ import annotations

import asyncio

import httpx
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations import vault
from app.models import Integration, IntegrationCredential


async def _google_integration(session: AsyncSession) -> Integration:
    row = Integration(
        slug="google-calendar",
        adapter="calendar",
        name="Google Calendar",
        scopes=["calendar:read", "calendar:act"],
        status="active",
        config={"provider": "google", "calendar_id": "primary"},
    )
    session.add(row)
    await session.flush()
    session.add(
        IntegrationCredential(
            integration_id=row.id,
            kind="oauth",
            encrypted_access=vault.encrypt("google-vault-token-123"),
            token_fingerprint=vault.fingerprint("google-vault-token-123"),
            scopes=["calendar:read", "calendar:act"],
        )
    )
    await session.commit()
    return row


async def test_gateway_calendar_write_uses_vault_and_provider_evidence(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    await _google_integration(db_session)
    calls: list[httpx.Request] = []
    created = {"value": False}

    async def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "GET":
            items = []
            if created["value"]:
                items = [
                    {
                        "id": "google-event-123",
                        "summary": "Dinner",
                        "start": {"dateTime": "2026-08-21T19:00:00+00:00"},
                    }
                ]
            return httpx.Response(200, json={"items": items})
        created["value"] = True
        assert request.headers["authorization"] == "Bearer google-vault-token-123"
        return httpx.Response(200, json={"id": "google-event-123"})

    monkeypatch.setattr(
        "app.integrations.adapters._make_client",
        lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    response = await client.post(
        "/v1/gateway/tools",
        json={
            "name": "calendar_add",
            "arguments": {
                "title": "Dinner",
                "start": "2026-08-21T19:00:00+00:00",
                "end": "2026-08-21T20:00:00+00:00",
                "confirm": True,
            },
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["ok"] is True
    assert result["event_id"] == "google-event-123"
    assert result["evidence"]["id"] == "google-event-123"
    assert any(request.method == "POST" for request in calls)


async def test_gateway_calendar_write_timeout_never_claims_creation(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    await _google_integration(db_session)

    async def slow(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"id": "too-late"})

    monkeypatch.setattr(
        "app.integrations.adapters._make_client",
        lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(slow)),
    )
    monkeypatch.setattr("app.ev.calendar_write.DEFAULT_TIMEOUT_SECONDS", 0.01)
    response = await client.post(
        "/v1/gateway/tools",
        json={
            "name": "calendar_add",
            "arguments": {
                "title": "Timeout event",
                "start": "2026-08-22T19:00:00+00:00",
                "end": "2026-08-22T20:00:00+00:00",
                "confirm": True,
            },
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["ok"] is False
    assert result["error"] == "timeout"
    assert "event_id" not in result


async def test_calendar_rejects_timezone_less_or_reversed_bounds(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    await _google_integration(db_session)
    for start, end in (
        ("2026-08-21T19:00:00", "2026-08-21T20:00:00"),
        ("2026-08-21T20:00:00+00:00", "2026-08-21T19:00:00+00:00"),
    ):
        response = await client.post(
            "/v1/gateway/tools",
            json={
                "name": "calendar_add",
                "arguments": {
                    "title": "Invalid event",
                    "start": start,
                    "end": end,
                    "confirm": True,
                },
            },
        )
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert result["ok"] is False
        assert result["error"] == "invalid_calendar_request"
