"""iPhone bootstrap idempotency: stable client id dedupes, camelCase accepted."""

from __future__ import annotations

from uuid import UUID

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Device


async def _device_ids(client: AsyncClient) -> list[str]:
    listed = await client.get("/v1/devices")
    assert listed.status_code == 200, listed.text
    return [row["id"] for row in listed.json()]


async def test_create_device_idempotent_on_client_device_id(client: AsyncClient) -> None:
    first = await client.post(
        "/v1/devices",
        json={"name": "iPhone 16 Pro", "capabilities": ["voice"], "client_device_id": "idfv-pro"},
    )
    assert first.status_code == 201, first.text
    first_body = first.json()
    assert first_body["device"]["client_device_id"] == "idfv-pro"

    # Reinstall/retry with the same stable id: same row back, rotated token.
    second = await client.post(
        "/v1/devices",
        json={"name": "iPhone 16 Pro", "capabilities": ["voice"], "client_device_id": "idfv-pro"},
    )
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["device"]["id"] == first_body["device"]["id"]
    assert second_body["token"] != first_body["token"]

    assert await _device_ids(client) == [first_body["device"]["id"]]


async def test_create_device_without_stable_id_keeps_always_create(
    client: AsyncClient,
) -> None:
    first = await client.post("/v1/devices", json={"name": "A", "capabilities": ["voice"]})
    second = await client.post("/v1/devices", json={"name": "B", "capabilities": ["voice"]})
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["device"]["id"] != second.json()["device"]["id"]
    assert len(await _device_ids(client)) == 2


async def test_create_device_persists_snake_case_device_type(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # Real wire format: EVAPIClient.encode uses convertToSnakeCase, so the phone
    # sends {"device_type": ...}; it must stick on the row and in the response.
    created = await client.post(
        "/v1/devices",
        json={"name": "iPhone SE", "capabilities": ["voice"], "device_type": "phone"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["device"]["device_type"] == "phone"
    row = await db_session.get(Device, UUID(created.json()["device"]["id"]))
    assert row is not None
    assert row.device_type == "phone"


async def test_revoked_stable_id_allows_fresh_create(client: AsyncClient) -> None:
    created = await client.post(
        "/v1/devices", json={"name": "Old", "client_device_id": "idfv-se"}
    )
    assert created.status_code == 201, created.text
    old_id = created.json()["device"]["id"]

    revoked = await client.delete(f"/v1/devices/{old_id}")
    assert revoked.status_code == 200, revoked.text

    again = await client.post(
        "/v1/devices", json={"name": "New", "client_device_id": "idfv-se"}
    )
    assert again.status_code == 201, again.text
    assert again.json()["device"]["id"] != old_id


async def test_panic_releases_stable_id_for_repair(client: AsyncClient) -> None:
    created = await client.post(
        "/v1/devices", json={"name": "Old", "client_device_id": "idfv-panic"}
    )
    assert created.status_code == 201, created.text
    old_id = created.json()["device"]["id"]

    panicked = await client.post(f"/v1/devices/{old_id}/panic", json={})
    assert panicked.status_code == 200, panicked.text

    again = await client.post(
        "/v1/devices", json={"name": "New", "client_device_id": "idfv-panic"}
    )
    assert again.status_code == 201, again.text
    assert again.json()["device"]["id"] != old_id
