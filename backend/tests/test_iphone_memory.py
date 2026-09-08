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


async def test_offline_queue_drop_own_device_only(owner_phone, gateway_phone) -> None:
    _body, owner = owner_phone
    _sbody, other = gateway_phone
    queued = await owner.post(
        "/v1/device-gateway/queue",
        json={"idempotency_key": "eac71-stuck-1", "kind": "siri_capture", "payload": {"text": "stuck note"}},
    )
    assert queued.status_code in {200, 201}, queued.text
    item_id = queued.json()["item"]["id"]

    foreign = await other.delete(f"/v1/device-gateway/queue/{item_id}")
    assert foreign.status_code == 404

    dropped = await owner.delete(f"/v1/device-gateway/queue/{item_id}")
    assert dropped.status_code == 200, dropped.text
    assert dropped.json()["dropped"] is True

    again = await owner.delete(f"/v1/device-gateway/queue/{item_id}")
    assert again.status_code == 200
    assert again.json()["idempotent"] is True


async def test_contacts_read_returns_snapshot(owner_phone) -> None:
    _body, phone = owner_phone
    posted = await phone.post(
        "/v1/device-gateway/contacts/snapshot",
        json={"contacts": [{"name": "Aarav Mehta"}, {"name": "Priya Shah"}], "captured_at": "2026-09-08T07:00:00Z"},
    )
    assert posted.status_code == 200
    res = await phone.get("/v1/device-gateway/contacts")
    assert res.status_code == 200, res.text
    body = res.json()
    assert [c["name"] for c in body["contacts"]] == ["Aarav Mehta", "Priya Shah"]
    assert body["sent_to_model"] is False
    assert body["captured_at"] == "2026-09-08T07:00:00Z"


async def test_look_history_owner_and_sandbox(
    owner_phone, gateway_phone, db_session
) -> None:
    from app.models import Event
    from app.utils.text import utcnow

    _body, owner = owner_phone
    _sbody, sandbox = gateway_phone
    db_session.add(
        Event(
            event_type="camera.look",
            source="owner.phone",
            content={"summary": "The kitchen counter with the basil plant", "scene": "kitchen"},
            occurred_at=utcnow(),
            sha256="f" * 64,
            device_id=str(owner_phone[0]["device"]["device_id"]),
        )
    )
    await db_session.commit()

    res = await owner.get("/v1/device-gateway/looks")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["memory_enabled"] is True
    assert len(body["looks"]) == 1
    assert "basil plant" in body["looks"][0]["summary"]
    assert body["looks"][0]["scene"] == "kitchen"

    sand = (await sandbox.get("/v1/device-gateway/looks")).json()
    assert sand["memory_enabled"] is False
    assert sand["looks"] == []


async def test_battery_report_persists_and_validates(gateway_phone) -> None:
    _body, phone = gateway_phone
    bad = await phone.post("/v1/device-gateway/battery", json={"percent": 140})
    assert bad.status_code == 422

    ok = await phone.post("/v1/device-gateway/battery", json={"percent": 73, "charging": True})
    assert ok.status_code == 200, ok.text
    assert ok.json()["percent"] == 73.0
    assert ok.json()["charging"] is True

    status = await phone.get("/v1/device-gateway/status")
    assert status.status_code == 200
    assert status.json()["battery_percent"] == 73.0


async def test_health_series_owner_and_phone_snapshot(owner_phone, db_session) -> None:
    from app.models import HealthSnapshot
    from app.utils.text import utcnow

    _body, phone = owner_phone
    db_session.add(
        HealthSnapshot(
            source="healthkit",
            metrics={"steps": 8123, "sleep_hours": 7.2},
            readiness=72.0,
            band="steady",
            occurred_at=utcnow(),
        )
    )
    await db_session.commit()
    snap = await phone.post(
        "/v1/device-gateway/healthkit/snapshot",
        json={"snapshot": {"steps": 400}, "captured_at": "2026-09-08T07:00:00Z", "available": True},
    )
    assert snap.status_code == 200

    res = await phone.get("/v1/device-gateway/vitals")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["phone_snapshot"]["metrics"] == {"steps": 400}
    assert len(body["series"]) == 1
    assert body["series"][0]["band"] == "steady"
    assert body["series"][0]["metrics"]["steps"] == 8123


async def test_weather_endpoint_structured(owner_phone, monkeypatch) -> None:
    _body, phone = owner_phone

    class FakeResult:
        title = "Surat"
        snippet = "27.5C overcast"

    async def fake_weather(_text, limit=2):
        return [FakeResult()]

    import app.device_gateway.api as api_mod

    monkeypatch.setattr("app.search.live.weather_results", fake_weather)
    # The endpoint imports weather_results inside the handler from
    # app.search.live, so patch the real module attribute.
    import app.search.live as live_mod

    monkeypatch.setattr(live_mod, "weather_results", fake_weather)
    res = await phone.get("/v1/device-gateway/weather", params={"place": "Surat"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "ok"
    assert body["forecast"]["title"] == "Surat"
    assert body["forecast"]["snippet"] == "27.5C overcast"
    assert body["error_code"] is None


async def test_inbox_ack_all_marks_read(owner_phone, db_session) -> None:
    from uuid import uuid4

    from app.everywhere.inbox import push_inbox

    _body, phone = owner_phone
    for i in range(2):
        await push_inbox(
            db_session,
            device_id=_body["device"]["device_id"],
            kind="digest",
            title="Evie digest",
            body="digest " + str(i),
            payload={},
        )
    await db_session.commit()
    res = await phone.post("/v1/device-gateway/inbox/ack-all")
    assert res.status_code == 200, res.text
    assert res.json()["acked"] == 2
    listed = (await phone.get("/v1/device-gateway/inbox")).json()
    assert listed["items"]
    assert all(item["unread"] is False for item in listed["items"])


async def test_capabilities_registry_trust_gated(
    owner_phone, gateway_phone
) -> None:
    _obody, owner = owner_phone
    _sbody, sandbox = gateway_phone

    sand = (await sandbox.get("/v1/device-gateway/capabilities")).json()
    assert sand["trust_state"] == "PAIRED_SANDBOX"
    assert sand["capabilities"]["memory"]["available"] is False
    assert sand["capabilities"]["memory"]["reason"] == "promote_on_mac"
    assert sand["capabilities"]["today"]["available"] is True
    assert sand["capabilities"]["people"]["reason"] == "no_contacts_snapshot"

    own = (await owner.get("/v1/device-gateway/capabilities")).json()
    assert own["trust_state"] == "TRUSTED_OWNER_DEVICE"
    assert own["capabilities"]["memory"]["available"] is True
    assert own["capabilities"]["capture_note"]["available"] is True


async def test_onboarding_roundtrip_filters_steps(gateway_phone) -> None:
    _body, phone = gateway_phone
    put = await phone.put(
        "/v1/device-gateway/onboarding",
        json={"steps_completed": ["paired", "camera_role", "hacked_step"], "camera_role_set": True},
    )
    assert put.status_code == 200, put.text
    body = put.json()["onboarding"]
    assert body["steps_completed"] == ["camera_role", "paired"]
    assert body["camera_role_set"] is True

    got = (await phone.get("/v1/device-gateway/onboarding")).json()["onboarding"]
    assert got["steps_completed"] == ["camera_role", "paired"]
    assert got["camera_role_set"] is True


async def test_pair_rate_limiter_throttles(client: AsyncClient) -> None:
    bad = {"pairing_token": "evie-pair.bogus-bogus-bogus", "display_name": "Spam"}
    got_429 = False
    for _ in range(24):
        res = await client.post("/v1/device-gateway/pair", json=bad)
        if res.status_code == 429:
            got_429 = True
            break
    assert got_429, "expected a 429 after repeated failed pairs"
    assert res.headers.get("X-Error-Code") == "pair_rate_limited"

    # A fresh pairing token still works after the window resets.
    from app.device_gateway import api as _gw_api

    _gw_api._PAIR_ATTEMPTS.clear()
    minted = await client.post(
        "/v1/device-gateway/pairing-tokens",
        json={"role": "primary_companion", "display_name": "After throttle"},
    )
    assert minted.status_code == 200
    ok = await client.post(
        "/v1/device-gateway/pair",
        json={"pairing_token": minted.json()["pairing_token"], "display_name": "After throttle"},
    )
    assert ok.status_code == 200
