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


async def test_search_endpoint_owner_and_sandbox(
    owner_phone, gateway_phone, client: AsyncClient, db_session
) -> None:
    from app.models import Event, Memory
    from app.utils.text import utcnow

    _body, owner = owner_phone
    _sbody, sandbox = gateway_phone

    mem = Memory(
        memory_type="preference",
        text="Sunset walks along Marine Drive",
        importance=0.7,
        confidence=0.9,
        source_type="explicit",
        fingerprint="eac67-search-mem",
        is_current=True,
    )
    db_session.add(mem)
    ev = Event(
        event_type="note",
        source="owner",
        content={"text": "Owner mentioned Marine Drive sunsets"},
        occurred_at=utcnow(),
        sha256="e" * 64,
    )
    db_session.add(ev)
    await db_session.commit()

    res = await owner.get("/v1/device-gateway/search", params={"q": "Marine Drive"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["memory_enabled"] is True
    kinds = [m["memory_type"] for m in body["memories"]]
    assert "preference" in kinds
    assert any("Marine Drive" in (e["text"] or "") for e in body["events"])

    sand = (await sandbox.get("/v1/device-gateway/search", params={"q": "Marine"})).json()
    assert sand["memory_enabled"] is False
    assert sand["memories"] == []
    assert sand["events"] == []


async def test_search_finds_reminders_and_contacts(owner_phone, db_session) -> None:
    from app.models import Alert

    _body, phone = owner_phone
    db_session.add(
        Alert(
            kind="reminder",
            title="Reminder",
            body="Water the basil plant",
            tier="useful",
            status="pending",
            source="set_reminder",
            fingerprint="eac67-alert",
        )
    )
    await db_session.commit()
    res = await phone.get("/v1/device-gateway/search", params={"q": "basil"})
    assert res.status_code == 200
    assert any("basil" in r["text"] for r in res.json()["reminders"])


async def test_capture_note_owner_and_sandbox_gate(
    owner_phone, gateway_phone, client: AsyncClient, db_session
) -> None:
    _body, owner = owner_phone
    _sbody, sandbox = gateway_phone

    denied = await sandbox.post(
        "/v1/device-gateway/capture",
        json={"text": "sneaky note", "idempotency_key": "sandbox-capture-1"},
    )
    assert denied.status_code == 403
    assert denied.headers.get("X-Error-Code") == "capture_requires_owner"

    res = await owner.post(
        "/v1/device-gateway/capture",
        json={"text": "Note: buy turmeric at the market", "idempotency_key": "eac69-note-1"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["kind"] == "note"
    assert body["duplicate"] is False

    dup = await owner.post(
        "/v1/device-gateway/capture",
        json={"text": "Note: buy turmeric at the market", "idempotency_key": "eac69-note-1"},
    )
    assert dup.status_code == 200
    assert dup.json()["duplicate"] is True
    assert dup.json()["event_id"] == body["event_id"]


async def test_audio_capture_stores_attachment(owner_phone, gateway_phone) -> None:
    import base64

    _body, owner = owner_phone
    _sbody, sandbox = gateway_phone
    tone = base64.b64encode(b"\x00\x01\x02RIFF-test-audio-bytes").decode("ascii")

    denied = await sandbox.post(
        "/v1/device-gateway/capture/audio",
        json={"audio_b64": tone, "content_type": "audio/mp4", "idempotency_key": "sandbox-audio-1"},
    )
    assert denied.status_code == 403

    res = await owner.post(
        "/v1/device-gateway/capture/audio",
        json={"audio_b64": tone, "content_type": "audio/mp4", "idempotency_key": "eac70-audio-1"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["kind"] == "voice_note"
    assert body["attachment_id"]
    assert body["size_bytes"] == len(b"\x00\x01\x02RIFF-test-audio-bytes")

    bad = await owner.post(
        "/v1/device-gateway/capture/audio",
        json={"audio_b64": "!!!not-base64!!!", "idempotency_key": "eac70-audio-bad"},
    )
    assert bad.status_code == 422
