"""iPhone memory browser — gateway /memories contract."""

from __future__ import annotations

from httpx import AsyncClient

from app.models import Memory


async def _seed_memory(session, *, text: str, memory_type: str = "fact") -> Memory:
    row = Memory(
        memory_type=memory_type,
        text=text,
        importance=0.7,
        confidence=0.9,
        source_type="explicit",
        privacy_level="normal",
        fingerprint="eac64-" + text[:24],
        is_current=True,
    )
    session.add(row)
    await session.flush()
    return row


async def test_memories_off_for_sandbox_phone(gateway_phone) -> None:
    _body, phone = gateway_phone
    res = await phone.get("/v1/device-gateway/memories")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["memory_enabled"] is False
    assert body["memories"] == []
    assert body["total"] == 0


async def test_memories_list_and_search(
    owner_phone, client: AsyncClient, db_session
) -> None:
    _body, phone = owner_phone
    await _seed_memory(db_session, text="Sahaj prefers espresso in the morning")
    await _seed_memory(db_session, text="Sahaj runs on Tuesdays", memory_type="routine")
    await db_session.commit()

    res = await phone.get("/v1/device-gateway/memories")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["memory_enabled"] is True
    assert body["total"] == 2
    assert {m["memory_type"] for m in body["memories"]} == {"fact", "routine"}
    assert all(m["id"] and m["text"] for m in body["memories"])

    searched = await phone.get(
        "/v1/device-gateway/memories", params={"q": "espresso"}
    )
    assert searched.status_code == 200
    hits = searched.json()["memories"]
    assert len(hits) >= 1
    assert "espresso" in hits[0]["text"].lower()

    typed = await phone.get(
        "/v1/device-gateway/memories", params={"memory_type": "routine"}
    )
    assert typed.status_code == 200
    assert [m["memory_type"] for m in typed.json()["memories"]] == ["routine"]


async def test_memory_detail_includes_provenance(
    owner_phone, db_session
) -> None:
    _body, phone = owner_phone
    mem = await _seed_memory(db_session, text="Evie remembers the walk")
    await db_session.commit()
    res = await phone.get(f"/v1/device-gateway/memories/{mem.id}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["memory"]["text"] == "Evie remembers the walk"
    assert "sources" in body


async def test_memory_requires_gateway_credential(client: AsyncClient) -> None:
    res = await client.get("/v1/device-gateway/memories")
    assert res.status_code == 401
