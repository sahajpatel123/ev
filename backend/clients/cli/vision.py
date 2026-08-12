"""`ev vision` subcommands: perception audit + user-confirmed labels."""

from __future__ import annotations

import httpx


async def list_perceptions(client: httpx.AsyncClient, *, limit: int = 50) -> list[dict]:
    resp = await client.get("/v1/vision/perceptions", params={"limit": limit})
    resp.raise_for_status()
    return resp.json()


async def list_pending(client: httpx.AsyncClient, *, limit: int = 50) -> list[dict]:
    resp = await client.get("/v1/vision/log", params={"source": "model", "limit": limit})
    resp.raise_for_status()
    return resp.json()


async def confirm_recognition(
    client: httpx.AsyncClient,
    recognition_id: str,
    *,
    entity_type: str = "thing",
) -> dict:
    resp = await client.post(
        f"/v1/vision/recognitions/{recognition_id}/confirm",
        json={"entity_type": entity_type},
    )
    resp.raise_for_status()
    return resp.json()


async def analyze_attachment(
    client: httpx.AsyncClient,
    attachment_id: str,
    *,
    allow_raw: bool = False,
    prompt: str | None = None,
) -> dict:
    resp = await client.post(
        "/v1/vision/analyze",
        json={
            "attachment_id": attachment_id,
            "permission": True,
            "allow_raw": allow_raw,
            "prompt": prompt,
        },
    )
    resp.raise_for_status()
    return resp.json()
