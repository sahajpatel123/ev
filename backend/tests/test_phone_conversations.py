"""Phone conversation sessions — the reviewable history surface.

Owner problem: the conversation sheet showed the phone's turn receipts as one
flat list, so a timer set on Tuesday sat next to this morning's chat with
nothing marking where one conversation ended. These tests pin the session
grouping (silence gap + live-session identity), the detail payload's honesty
about replies that were never recorded, and the PWA wiring that renders it.

Grouping is derived at read time from immutable receipts, so the same rows
always produce the same sessions — the first receipt's id names its session.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.models import Device, Event, PhoneTurnReceipt
from app.utils.text import utcnow


def _client() -> AsyncClient:
    from app.main import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _pair_sandbox(client: AsyncClient, name: str) -> AsyncClient:
    from app.main import app

    minted = await client.post(
        "/v1/device-gateway/pairing-tokens",
        json={"role": "companion", "display_name": name},
    )
    assert minted.status_code == 200, minted.text
    phone = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    paired = await phone.post(
        "/v1/device-gateway/pair",
        json={
            "pairing_token": minted.json()["pairing_token"],
            "display_name": name,
            "protocol_version": "1",
            "client_version": "2026.10.08.05",
            "platform": "ios",
            "instance_id": name + "-tab",
        },
    )
    assert paired.status_code == 200, paired.text
    phone.headers["Authorization"] = f"Bearer {paired.json()['device_token']}"
    phone.headers["X-Forwarded-Proto"] = "https"
    return phone


async def _device_of(db_session, name: str) -> Device:
    row = (await db_session.execute(select(Device).where(Device.name == name))).scalar_one()
    return row


async def _receipt(
    db_session,
    *,
    device: Device,
    text: str,
    minutes_ago: int,
    session_id: str | None = None,
    reply: str | None = None,
    action_calls: list | None = None,
    realtime: bool = False,
) -> PhoneTurnReceipt:
    """One durable receipt as the turn paths would have written it."""

    evidence: dict = {}
    if reply is not None:
        evidence = {"core_takeover": True, "core_reply": reply, "core_route": "kernel"}
    if realtime:
        evidence = {"response_owner": "realtime"}
    row = PhoneTurnReceipt(
        device_id=device.id,
        idempotency_key=uuid4().hex,
        kind="text",
        transcript=text,
        session_id=session_id,
        action_calls=list(action_calls or []),
        evidence=evidence,
        trusted_owner=True,
        created_at=utcnow() - timedelta(minutes=minutes_ago),
    )
    db_session.add(row)
    await db_session.flush()
    return row


async def _event(
    db_session,
    *,
    device: Device,
    text: str,
    at: datetime,
    event_type: str = "message.user",
    privacy_level: str = "sensitive",
) -> Event:
    """One streamed typed turn as the chat pipeline records it: the owner's
    message and Evie's answer, on this device, on the default thread."""

    row = Event(
        occurred_at=at,
        source="device_text",
        event_type=event_type,
        content={"text": text},
        device_id=str(device.id),
        privacy_level=privacy_level,
        sha256=uuid4().hex + uuid4().hex,
    )
    db_session.add(row)
    await db_session.flush()
    return row


async def test_silence_splits_sessions(client, db_session):
    """A 3-hour-old exchange and this morning's chat are two conversations."""

    phone = await _pair_sandbox(client, "Sess-Split")
    device = await _device_of(db_session, "Sess-Split")
    await _receipt(db_session, device=device, text="set a timer for the laundry", minutes_ago=200)
    await _receipt(
        db_session, device=device, text="what does Priya prefer for coffee", minutes_ago=20,
        reply="Oat latte, no sugar.", action_calls=[{"name": "memory_read", "route": "CORE", "executed": True}],
    )
    await db_session.commit()

    body = (await phone.get("/v1/device-gateway/conversations")).json()
    assert body["ok"] is True
    sessions = body["sessions"]
    assert len(sessions) == 2, sessions
    # Newest first, and each card is titled by its own first owner line.
    assert sessions[0]["title"] == "what does Priya prefer for coffee"
    assert sessions[0]["turns"] == 1
    assert sessions[0]["preview"] == "what does Priya prefer for coffee"
    assert sessions[1]["title"] == "set a timer for the laundry"
    assert sessions[0]["last_at"] > sessions[1]["last_at"]


async def test_close_turns_stay_one_session(client, db_session):
    """Minutes apart is one sitting: the detail shows the whole exchange."""

    phone = await _pair_sandbox(client, "Sess-Close")
    device = await _device_of(db_session, "Sess-Close")
    await _receipt(db_session, device=device, text="what is on my calendar today", minutes_ago=12)
    await _receipt(db_session, device=device, text="move the standup to 4", minutes_ago=9)
    await db_session.commit()

    body = (await phone.get("/v1/device-gateway/conversations")).json()
    assert len(body["sessions"]) == 1
    session_id = body["sessions"][0]["id"]
    assert body["sessions"][0]["turns"] == 2

    detail = (await phone.get(f"/v1/device-gateway/conversations/{session_id}")).json()
    assert detail["ok"] is True
    assert detail["session"]["id"] == session_id
    turns = detail["turns"]
    assert [t["owner_text"] for t in turns] == [
        "what is on my calendar today",
        "move the standup to 4",
    ]
    # Chronological: the oldest turn of the sitting is the first row.
    assert turns[0]["at"] <= turns[1]["at"]


async def test_live_session_identity_survives_silence(client, db_session):
    """One live voice session is one conversation even across a long pause;
    two different live sessions are two conversations."""

    phone = await _pair_sandbox(client, "Sess-Live")
    device = await _device_of(db_session, "Sess-Live")
    await _receipt(db_session, device=device, text="hey Evie", minutes_ago=300, session_id="live-a")
    await _receipt(db_session, device=device, text="and one more thing", minutes_ago=280, session_id="live-a")
    await _receipt(db_session, device=device, text="new conversation", minutes_ago=60, session_id="live-b")
    await db_session.commit()

    body = (await phone.get("/v1/device-gateway/conversations")).json()
    sessions = body["sessions"]
    assert len(sessions) == 2, sessions
    by_title = {s["title"]: s for s in sessions}
    assert by_title["hey Evie"]["turns"] == 2
    assert by_title["hey Evie"]["live"] is True
    assert by_title["new conversation"]["turns"] == 1


async def test_session_detail_marks_unrecorded_replies(client, db_session):
    """A reply the server never stored is reported as not recorded. The
    surface may not echo the owner's own words back as Evie's answer."""

    phone = await _pair_sandbox(client, "Sess-Honest")
    device = await _device_of(db_session, "Sess-Honest")
    await _receipt(db_session, device=device, text="tell me a joke", minutes_ago=5, realtime=True)
    await _receipt(
        db_session, device=device, text="text Priya that I am late", minutes_ago=4,
        reply="Told Priya you are on your way.",
        action_calls=[{"name": "send_message", "route": "HOME_STATION", "executed": True}],
    )
    await db_session.commit()

    body = (await phone.get("/v1/device-gateway/conversations")).json()
    session_id = body["sessions"][0]["id"]
    detail = (await phone.get(f"/v1/device-gateway/conversations/{session_id}")).json()
    turns = detail["turns"]
    assert turns[0]["owner_text"] == "tell me a joke"
    assert turns[0]["reply_recorded"] is False
    assert turns[0]["reply_text"] is None
    # The server says why the reply is missing; the client shows exactly this.
    assert "live" in turns[0]["reply_note"].lower()
    assert turns[1]["reply_recorded"] is True
    assert turns[1]["reply_text"] == "Told Priya you are on your way."
    assert turns[1]["chips"][0]["tool"] == "send_message"
    assert turns[1]["chips"][0]["executed"] is True


async def test_session_detail_404_for_unknown_id(client, db_session):
    """A session id this phone never had is a 404, not an empty session."""

    phone = await _pair_sandbox(client, "Sess-404")
    missing = await phone.get("/v1/device-gateway/conversations/sess-" + "0" * 32)
    assert missing.status_code == 404


async def test_sessions_surface_answers_every_trust_state(client, db_session):
    """Transparency is universal: the sandbox phone gets the same honest
    read-only answer shape as the owner's phone."""

    sandbox = await _pair_sandbox(client, "Sess-Sandbox")
    r = await sandbox.get("/v1/device-gateway/conversations")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["sessions"] == []


async def test_gap_setting_controls_the_boundary(client, db_session, monkeypatch):
    """A larger gap keeps a resumed conversation together; zero splits each
    turn into its own session. The rule is the setting, not a constant."""

    from app.config import settings

    phone = await _pair_sandbox(client, "Sess-Gap")
    device = await _device_of(db_session, "Sess-Gap")
    await _receipt(db_session, device=device, text="first thought", minutes_ago=100)
    await _receipt(db_session, device=device, text="resumed later", minutes_ago=30)
    await db_session.commit()

    original = settings.phone_session_gap_seconds
    try:
        monkeypatch.setattr(settings, "phone_session_gap_seconds", 7200)
        wide = (await phone.get("/v1/device-gateway/conversations")).json()
        assert len(wide["sessions"]) == 1
        assert wide["sessions"][0]["turns"] == 2

        monkeypatch.setattr(settings, "phone_session_gap_seconds", 0)
        narrow = (await phone.get("/v1/device-gateway/conversations")).json()
        assert len(narrow["sessions"]) == 2
    finally:
        settings.phone_session_gap_seconds = original


async def test_pwa_renders_sessions_not_a_flat_turn_list():
    """The conversation sheet shows tappable sessions; the flat
    "Recent turns on this phone" list is gone."""

    with open("clients/pwa/index.html") as handle:
        html = handle.read()
    assert "Recent turns on this phone" not in html
    assert 'id="session-list"' in html
    assert 'id="session-sheet"' in html

    with open("clients/pwa/app.js") as handle:
        js = handle.read()
    assert "loadSessionList" in js
    assert "openSession" in js
    assert "/v1/device-gateway/conversations" in js

    with open("clients/pwa/style.css") as handle:
        css = handle.read()
    assert ".session-card" in css


async def test_receipt_sessions_are_stable_under_new_turns(client, db_session):
    """Session ids anchor on the first durable row, so appending today's turn
    cannot renumber a session the owner already opened."""

    phone = await _pair_sandbox(client, "Sess-Stable")
    device = await _device_of(db_session, "Sess-Stable")
    old = await _receipt(db_session, device=device, text="yesterday's plan", minutes_ago=1500)
    await db_session.commit()
    first = (await phone.get("/v1/device-gateway/conversations")).json()["sessions"][0]
    assert first["id"] == f"sess-{old.created_at.strftime('%Y%m%d%H%M%S')}-{str(old.id)[-8:]}"

    await _receipt(db_session, device=device, text="this morning's plan", minutes_ago=30)
    await db_session.commit()
    again = (await phone.get("/v1/device-gateway/conversations")).json()["sessions"]
    assert again[1]["id"] == first["id"]
    assert again[0]["id"] != first["id"]


async def test_typed_chat_events_join_the_history_with_replies(client, db_session):
    """The PWA composer's turns are recorded as thread events, not receipts;
    without them the phone's history would silently miss most of what the
    owner typed. One typed exchange reads as owner ask + Evie's answer."""

    phone = await _pair_sandbox(client, "Sess-Typed")
    device = await _device_of(db_session, "Sess-Typed")
    base = datetime.now(UTC) - timedelta(hours=2)
    await _event(
        db_session, device=device, text="what time is my flight", at=base,
        event_type="message.user",
    )
    await _event(
        db_session, device=device, text="6:40 AM to Denver.", at=base + timedelta(seconds=9),
        event_type="message.assistant",
    )
    await db_session.commit()

    body = (await phone.get("/v1/device-gateway/conversations")).json()
    assert len(body["sessions"]) == 1
    assert body["sessions"][0]["turns"] == 1
    detail = (await phone.get(f"/v1/device-gateway/conversations/{body['sessions'][0]['id']}")).json()
    turn = detail["turns"][0]
    assert turn["origin"] == "owner"
    assert turn["owner_text"] == "what time is my flight"
    assert turn["reply_recorded"] is True
    assert turn["reply_text"] == "6:40 AM to Denver."


async def test_never_send_to_model_events_stay_out_of_the_surface(client, db_session):
    """The privacy boundary strips these everywhere, including history."""

    phone = await _pair_sandbox(client, "Sess-Private")
    device = await _device_of(db_session, "Sess-Private")
    await _event(
        db_session, device=device, text="my password is hunter2", at=utcnow(),
        event_type="message.user", privacy_level="never_send_to_model",
    )
    await db_session.commit()

    body = (await phone.get("/v1/device-gateway/conversations")).json()
    assert body["sessions"] == []


async def test_evies_own_words_are_hers_not_the_owners(client, db_session):
    """An assistant event with no recorded ask is Evie's line — never glued
    onto someone else's question."""

    phone = await _pair_sandbox(client, "Sess-Evie")
    device = await _device_of(db_session, "Sess-Evie")
    await _event(
        db_session, device=device, text="Good morning. Your first meeting moved.", at=utcnow(),
        event_type="message.assistant",
    )
    await db_session.commit()

    body = (await phone.get("/v1/device-gateway/conversations")).json()
    session_id = body["sessions"][0]["id"]
    detail = (await phone.get(f"/v1/device-gateway/conversations/{session_id}")).json()
    turn = detail["turns"][0]
    assert turn["origin"] == "evie"
    assert turn["owner_text"] == ""
    assert turn["reply_text"] == "Good morning. Your first meeting moved."


async def test_voice_then_typing_stays_one_session(client, db_session):
    """A live voice turn followed by a typed question in the same sitting is
    one conversation, not two fragments."""

    phone = await _pair_sandbox(client, "Sess-Mixed")
    device = await _device_of(db_session, "Sess-Mixed")
    await _receipt(
        db_session, device=device, text="hey Evie, look at this", minutes_ago=5,
        session_id="live-x", reply="Looking now.",
    )
    await _event(
        db_session, device=device, text="and text Priya about it", at=utcnow() - timedelta(minutes=2),
        event_type="message.user",
    )
    await db_session.commit()

    body = (await phone.get("/v1/device-gateway/conversations")).json()
    assert len(body["sessions"]) == 1
    assert body["sessions"][0]["turns"] == 2
    assert body["sessions"][0]["live"] is True


async def test_session_detail_orders_rows_within_a_session(client, db_session):
    """The detail view is the exchange in order, so the owner reads what was
    said and what Evie answered, per turn."""

    phone = await _pair_sandbox(client, "Sess-Order")
    device = await _device_of(db_session, "Sess-Order")
    base = datetime.now(UTC) - timedelta(hours=2)
    rows = [
        ("what time is my flight", "6:40 AM to Denver.", 0),
        ("switch it to the evening", "Moved to 8:15 PM.", 10),
        ("thanks", "You're welcome.", 18),
    ]
    for text, reply, minutes in rows:
        row = PhoneTurnReceipt(
            device_id=device.id,
            idempotency_key=uuid4().hex,
            kind="text",
            transcript=text,
            evidence={"core_takeover": True, "core_reply": reply},
            trusted_owner=True,
            created_at=base + timedelta(minutes=minutes),
        )
        db_session.add(row)
    await db_session.commit()

    body = (await phone.get("/v1/device-gateway/conversations")).json()
    session_id = body["sessions"][0]["id"]
    detail = (await phone.get(f"/v1/device-gateway/conversations/{session_id}")).json()
    assert [(t["owner_text"], t["reply_text"]) for t in detail["turns"]] == [
        (text, reply) for text, reply, _ in rows
    ]


async def test_empty_session_gets_a_dated_title_not_a_blank_card(client, db_session):
    """A sitting with no words on either side still renders a card the owner
    can recognize and open."""

    phone = await _pair_sandbox(client, "Sess-Title")
    device = await _device_of(db_session, "Sess-Title")
    await _receipt(db_session, device=device, text="", minutes_ago=5)
    await db_session.commit()

    body = (await phone.get("/v1/device-gateway/conversations")).json()
    assert len(body["sessions"]) == 1
    assert body["sessions"][0]["title"].startswith("Conversation · ")
    detail = (await phone.get(f"/v1/device-gateway/conversations/{body['sessions'][0]['id']}")).json()
    assert detail["ok"] is True
    assert len(detail["turns"]) == 1


async def test_late_assistant_event_is_evies_line_not_a_glued_reply(client, db_session):
    """An assistant event landing long after the ask is proactive, not the
    answer to that question — it must not be glued onto it."""

    phone = await _pair_sandbox(client, "Sess-Late")
    device = await _device_of(db_session, "Sess-Late")
    base = datetime.now(UTC) - timedelta(hours=2)
    await _event(
        db_session, device=device, text="any news on the flight", at=base,
        event_type="message.user",
    )
    await _event(
        db_session, device=device, text="Boarding started.", at=base + timedelta(minutes=31),
        event_type="message.assistant",
    )
    await db_session.commit()

    body = (await phone.get("/v1/device-gateway/conversations")).json()
    assert len(body["sessions"]) == 1
    detail = (await phone.get(f"/v1/device-gateway/conversations/{body['sessions'][0]['id']}")).json()
    turns = detail["turns"]
    assert len(turns) == 2
    assert turns[0]["reply_recorded"] is False
    assert turns[1]["origin"] == "evie"
    assert turns[1]["reply_text"] == "Boarding started."


async def test_same_second_sessions_stay_distinct(client, db_session):
    """Two sittings that begin in the same second never share a session id:
    the key is anchored on the first durable row, not just the clock."""

    phone = await _pair_sandbox(client, "Sess-Anchor")
    device = await _device_of(db_session, "Sess-Anchor")
    base = datetime.now(UTC) - timedelta(hours=1)
    for text, live in (("first call", "live-1"), ("second call", "live-2")):
        db_session.add(
            PhoneTurnReceipt(
                device_id=device.id,
                idempotency_key=uuid4().hex,
                kind="voice",
                transcript=text,
                session_id=live,
                trusted_owner=True,
                created_at=base,
            )
        )
    await db_session.commit()

    body = (await phone.get("/v1/device-gateway/conversations")).json()
    sessions = body["sessions"]
    assert len(sessions) == 2
    assert sessions[0]["id"] != sessions[1]["id"]
    prefix = "sess-" + base.strftime("%Y%m%d%H%M%S")
    assert sessions[0]["id"].startswith(prefix)
    assert sessions[1]["id"].startswith(prefix)


async def test_session_list_respects_limit(client, db_session):
    """The phone asks for a bounded list and gets at most that many cards,
    newest first."""

    phone = await _pair_sandbox(client, "Sess-Limit")
    device = await _device_of(db_session, "Sess-Limit")
    await _receipt(db_session, device=device, text="oldest thought", minutes_ago=200)
    await _receipt(db_session, device=device, text="middle thought", minutes_ago=100)
    await _receipt(db_session, device=device, text="newest thought", minutes_ago=5)
    await db_session.commit()

    one = (await phone.get("/v1/device-gateway/conversations?limit=1")).json()
    assert [s["title"] for s in one["sessions"]] == ["newest thought"]
    two = (await phone.get("/v1/device-gateway/conversations?limit=2")).json()
    assert [s["title"] for s in two["sessions"]] == ["newest thought", "middle thought"]
