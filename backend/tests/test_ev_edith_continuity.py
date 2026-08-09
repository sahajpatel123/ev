"""Tests for continuous conversation, live data recording, and E.D.I.T.H. modules."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev import conversation
from app.ev.live import query_live_events
from app.ev.rollup import build_rollup, model_safe_rollup
from app.models import ConversationThread
from app.schemas import EventCreate
from app.services.event_service import EventService


async def post_event(client: AsyncClient, text: str) -> dict:
    resp = await client.post(
        "/v1/events",
        json={"source": "test", "event_type": "note", "text": text},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["event"]


async def test_single_continuous_conversation_window(client: AsyncClient) -> None:
    first = await client.post("/v1/chat", json={"message": "I decided to use SQLite for testing."})
    assert first.status_code == 200, first.text
    conversation_id = first.json()["conversation_id"]
    assert conversation_id is not None

    second = await client.post("/v1/chat", json={"message": "Continue — what was I doing?"})
    assert second.status_code == 200, second.text
    assert second.json()["conversation_id"] == conversation_id

    resp = await client.get("/v1/conversation")
    assert resp.status_code == 200, resp.text
    detail = resp.json()
    assert detail["conversation"]["id"] == conversation_id
    assert detail["conversation"]["is_default"] is True
    roles = [m["role"] for m in detail["messages"]]
    texts = [m["text"] for m in detail["messages"]]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert texts[0] == "I decided to use SQLite for testing."
    assert texts[2] == "Continue — what was I doing?"
    assert detail["state"]["focus"] is not None

    resp = await client.get("/v1/conversations")
    assert len(resp.json()) == 1

    resp = await client.post("/v1/conversation/reset", json={"reason": "starting a fresh topic"})
    assert resp.status_code == 200
    assert resp.json()["focus"] is None

    resp = await client.get("/v1/conversation")
    assert resp.json()["state"]["focus"] is None
    assert len(resp.json()["messages"]) == 4  # history preserved, only working state reset


async def test_history_returns_most_recent_window(db_session: AsyncSession) -> None:
    thread = await conversation.get_default_thread(db_session)
    service = EventService(db_session, actor="test")
    for i in range(25):
        await service.create(
            EventCreate(
                source="chat",
                event_type="message.user",
                text=f"message {i}",
                conversation_id=thread.id,
            )
        )
        await service.create(
            EventCreate(
                source="chat",
                event_type="message.assistant",
                text=f"reply {i}",
                conversation_id=thread.id,
            )
        )
    await db_session.flush()

    recent = await conversation.history(db_session, thread.id, limit=10)
    texts = [(event.content or {}).get("text") for event in recent]
    assert len(texts) == 10
    assert texts[0] == "message 20"
    assert texts[-1] == "reply 24"
    assert "message 0" not in texts
    assert "reply 0" not in texts


async def test_conversation_endpoint_returns_recent_window(client: AsyncClient) -> None:
    await client.post("/v1/chat", json={"message": "message 0"})
    for i in range(1, 12):
        await client.post("/v1/chat", json={"message": f"message {i}"})

    resp = await client.get("/v1/conversation?limit=4")
    assert resp.status_code == 200, resp.text
    messages = resp.json()["messages"]
    assert len(messages) == 4
    assert messages[0]["text"] == "message 10"
    assert "message 11" in messages[-1]["text"]


async def test_single_default_thread_invariant(db_session: AsyncSession) -> None:
    first = await conversation.get_default_thread(db_session)
    second = await conversation.get_default_thread(db_session)
    assert first.id == second.id

    threads = (
        await db_session.execute(select(ConversationThread))
    ).scalars().all()
    assert sum(1 for t in threads if t.is_default) == 1

    # The partial unique index rejects a second default row even when bypassing
    # get_default_thread.
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(ConversationThread(title="duplicate", is_default=True))
            await db_session.flush()


async def test_chat_response_includes_context_plan(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/chat",
        json={"message": "I decided to use SQLite for testing."},
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    plan = payload["context_plan"]
    assert plan is not None
    assert plan["used_tokens"] == payload["context_tokens"]
    assert plan["budget"] > 0
    assert plan["remaining_tokens"] >= 0
    assert plan["over_budget"] is False
    names = [section["name"] for section in plan["sections"]]
    assert "strategy" in names
    assert "user_state" in names
    assert "retrieved_memory" in names


async def test_rolling_summary_and_progressive_depth(client: AsyncClient) -> None:
    first = await client.post(
        "/v1/chat",
        json={"message": "I decided to build the EV memory engine with SQLite first."},
    )
    assert first.status_code == 200, first.text
    conversation_id = first.json()["conversation_id"]

    await client.post(
        "/v1/chat",
        json={"message": "Now I'm implementing the retrieval ranking algorithm."},
    )
    await client.post(
        "/v1/chat",
        json={"message": "What latency budget should the quick-card path meet?"},
    )

    resp = await client.get("/v1/conversation")
    assert resp.status_code == 200, resp.text
    detail = resp.json()
    rollup = detail["rollup"]
    assert rollup is not None
    assert rollup["covered_turn_count"] >= 3
    assert "ROLLING CONVERSATION SUMMARY" in rollup["summary"]
    assert "SQLite" in rollup["summary"]
    assert "retrieval" in rollup["summary"].lower()
    assert "ranking" in rollup["summary"].lower()
    assert "latency" in rollup["summary"].lower()
    assert "budget" in rollup["summary"].lower()
    assert rollup["token_count"] <= 4000  # bounded, structured text

    resp = await client.post("/v1/chat", json={"message": "Hello there."})
    assert resp.json()["context_depth"] == "standard"

    resp = await client.post(
        "/v1/chat",
        json={"message": "Continue — where were we?"},
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["conversation_id"] == conversation_id
    assert payload["context_depth"] == "deep"

    resp = await client.post(
        "/v1/chat",
        json={"message": "Give me the deepest resume context.", "context_depth": "deepest"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["context_depth"] == "deepest"

    # Open questions are tracked in state and surfaced by the conversation view.
    state = resp.json()
    assert state["context_tokens"] > 0
    conv = (await client.get("/v1/conversation")).json()
    assert any("latency budget" in q for q in conv["state"]["pending_questions"])


async def test_continue_uses_default_thread_and_rollup(client: AsyncClient) -> None:
    resp = await client.post("/v1/chat", json={"message": "Building the EV wrist unit case."})
    conversation_id = resp.json()["conversation_id"]
    await client.post(
        "/v1/chat",
        json={"message": "The OLED panel is on shelf A and needs a bezel design."},
    )

    resp = await client.post("/v1/continue")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["conversation_id"] == conversation_id
    assert payload["summary"] is not None
    assert "wrist" in payload["summary"].lower()
    assert "unit" in payload["summary"].lower()
    assert payload["recent_context"]
    assert payload["next_actions"]


async def test_tombstone_rebuilds_rollup_without_redacted_text(client: AsyncClient) -> None:
    await client.post("/v1/chat", json={"message": "Qubit-9 is the secret project codename."})

    resp = await client.get("/v1/conversation")
    assert "Qubit-9" in resp.json()["rollup"]["summary"]

    timeline = (await client.get("/v1/timeline?event_type=message.user")).json()
    secret_event = next(e for e in timeline["events"] if "Qubit-9" in e["content"]["text"])
    resp = await client.delete(f"/v1/events/{secret_event['id']}?reason=user-requested")
    assert resp.status_code == 200, resp.text

    resp = await client.get("/v1/conversation")
    assert "Qubit-9" not in resp.json()["rollup"]["summary"]


async def test_model_safe_rollup_excludes_never_send_content(db_session: AsyncSession) -> None:
    thread = await conversation.get_default_thread(db_session)
    service = EventService(db_session, actor="test")
    await service.create(
        EventCreate(
            source="chat",
            event_type="message.user",
            text="Working on the retrieval ranking algorithm.",
            conversation_id=thread.id,
        )
    )
    await service.create(
        EventCreate(
            source="chat",
            event_type="message.assistant",
            text="EV: mock reply.",
            conversation_id=thread.id,
        )
    )
    await service.create(
        EventCreate(
            source="chat",
            event_type="message.user",
            text="Qubit-9 is the secret project codename.",
            conversation_id=thread.id,
            privacy_level="never_send_to_model",
        )
    )
    await db_session.flush()

    rollup = await build_rollup(db_session, thread.id)
    assert "Qubit-9" in rollup.summary
    assert "retrieval ranking" in rollup.summary.lower()

    safe = await model_safe_rollup(db_session, thread.id)
    assert "Qubit-9" not in safe.summary
    assert not any("Qubit-9" in q for q in safe.open_questions)
    assert "retrieval ranking" in safe.summary.lower()


async def test_live_data_recording_and_state_feed(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/live/channels",
        json={"name": "screen-activity", "kind": "screen", "metadata": {"collector": "mac"}},
    )
    assert resp.status_code == 201, resp.text
    channel_id = resp.json()["id"]

    resp = await client.post(
        f"/v1/live/channels/{channel_id}/events",
        json=[
            {"event_type": "focus_change", "payload": {"app": "Xcode", "text": "editing retrieval.py"}},
            {"event_type": "idle", "payload": {"minutes": 4}},
        ],
    )
    assert resp.status_code == 201, resp.text
    assert len(resp.json()) == 2

    resp = await client.post(
        "/v1/live/events",
        json={
            "channel": "health-belt",
            "kind": "health",
            "events": [
                {"event_type": "heart_rate", "payload": {"bpm": 72, "text": "steady"}},
                {"event_type": "step_count", "payload": {"steps": 6123}},
            ],
        },
    )
    assert resp.status_code == 201, resp.text

    resp = await client.get("/v1/live/status")
    assert resp.status_code == 200
    status = resp.json()
    assert status["total_events_24h"] == 4
    assert {c["channel"]["name"] for c in status["channels"]} == {"screen-activity", "health-belt"}

    resp = await client.get("/v1/state")
    assert resp.status_code == 200
    assert any("heart_rate" in line for line in resp.json()["live_context"])


async def test_live_permission_fail_closed_and_replay_idempotent(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    resp = await client.post(
        "/v1/live/channels",
        json={
            "name": "private-screen",
            "kind": "screen",
            "privacy_level": "never_send_to_model",
            "metadata": {"collector": "mac-sensor"},
        },
    )
    assert resp.status_code == 201, resp.text
    channel_id = resp.json()["id"]

    batch = [
        {
            "event_type": "focus_change",
            "payload": {"app": "SecretApp", "text": "top-secret screen text"},
            "privacy_level": "normal",
            "occurred_at": "2026-08-09T10:00:00Z",
        }
    ]
    resp = await client.post(f"/v1/live/channels/{channel_id}/events", json=batch)
    assert resp.status_code == 201, resp.text
    event = resp.json()[0]
    # Channel permission is fail-closed: the event is stored at the channel level.
    assert event["privacy_level"] == "never_send_to_model"
    assert event["collector"] == "mac-sensor"

    # Replaying the same batch is idempotent; no duplicate rows.
    resp = await client.post(f"/v1/live/channels/{channel_id}/events", json=batch)
    assert resp.status_code == 201, resp.text
    assert resp.json() == []
    resp = await client.get(f"/v1/live/channels/{channel_id}/events")
    assert len(resp.json()) == 1

    # The user can still see their own live data (derived, never raw screen
    # pixels even in the user-facing state line)...
    resp = await client.get("/v1/state")
    assert resp.status_code == 200
    state_lines = [
        line for line in resp.json()["live_context"] if "private-screen" in line
    ]
    assert state_lines
    assert "screen app=SecretApp" in state_lines[0]
    assert "top-secret screen text" not in state_lines[0]

    # ...but the model-facing slice excludes the channel and its events entirely.
    user_rows = await query_live_events(db_session, access="user")
    model_rows = await query_live_events(db_session, access="model")
    assert len(user_rows) == 1
    assert model_rows == []


async def test_ev_sense_consumes_permissioned_live_signals(client: AsyncClient) -> None:
    late = (datetime.now(UTC) - timedelta(days=1)).replace(
        hour=23, minute=30, second=0, microsecond=0
    )
    resp = await client.post(
        "/v1/live/events",
        json={
            "channel": "screen-activity",
            "kind": "screen",
            "events": [
                {
                    "event_type": "focus_change",
                    "payload": {"app": "Xcode", "text": "editing retrieval.py"},
                    "occurred_at": late.isoformat(),
                },
                {
                    "event_type": "focus_change",
                    "payload": {"app": "Notes", "text": "notes"},
                    "occurred_at": late.isoformat(),
                },
            ],
        },
    )
    assert resp.status_code == 201, resp.text

    resp = await client.post(
        "/v1/live/events",
        json={
            "channel": "health-belt",
            "kind": "health",
            "events": [
                {
                    "event_type": "heart_rate",
                    "payload": {"bpm": 132, "text": "elevated"},
                    "occurred_at": late.isoformat(),
                }
            ],
        },
    )
    assert resp.status_code == 201, resp.text

    resp = await client.post("/v1/sense/predict", json={"window_days": 30})
    assert resp.status_code == 200, resp.text
    predictions = {p["kind"]: p for p in resp.json()["predictions"]}
    assert "screen_late_night" in predictions
    assert "live_health_signal" in predictions
    assert predictions["screen_late_night"]["basis_ids"]
    assert predictions["live_health_signal"]["basis_ids"]
    assert "23:00" in predictions["screen_late_night"]["why_now"]


async def test_focus_designation_and_hud_overlay(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/focus",
        json={"label": "Ship EV memory engine", "kind": "goal", "reason": "E.D.I.T.H.-style lock-on"},
    )
    assert resp.status_code == 201, resp.text
    focus_id = resp.json()["id"]

    resp = await client.get("/v1/focus")
    assert resp.status_code == 200
    assert resp.json()["label"] == "Ship EV memory engine"

    resp = await client.get("/v1/hud/focus")
    assert resp.status_code == 200
    overlay = resp.json()
    assert overlay["schema_version"] == "ev.hud.focus.v1"
    assert overlay["locked"] is True
    assert overlay["focus"]["id"] == focus_id
    assert overlay["next_action"] == "Advance goal: Ship EV memory engine"

    resp = await client.post(f"/v1/focus/{focus_id}/end")
    assert resp.status_code == 200
    assert resp.json()["active"] is False


async def test_fleet_status_and_tasks(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/devices",
        json={"name": "iphone-16-pro", "capabilities": ["voice", "camera", "health", "capture_photo"]},
    )
    assert resp.status_code == 201, resp.text
    device_id = resp.json()["device"]["id"]

    resp = await client.get("/v1/fleet")
    assert resp.status_code == 200
    fleet = resp.json()
    assert any(d["device_id"] == device_id for d in fleet["devices"])

    resp = await client.post(
        "/v1/fleet/tasks",
        json={"device_id": device_id, "task_type": "capture_photo", "payload": {"subject": "workbench"}},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "requested"

    resp = await client.get("/v1/fleet/tasks")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


async def test_recognition_log_and_person_link(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/vision/annotate",
        json={"label": "Maya", "entity_type": "person", "confidence": 0.95, "source": "user"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["entity_id"] is not None

    resp = await client.get("/v1/vision/log")
    assert resp.status_code == 200
    assert resp.json()[0]["label"] == "Maya"

    resp = await client.get("/v1/people/Maya/whereabouts")
    assert resp.status_code == 200
    assert resp.json()["entity_id"] is not None


async def test_ops_center_and_digital_twin(client: AsyncClient) -> None:
    await post_event(client, "I decided to use SQLite for local testing.")
    await post_event(client, "I want to build EV as a persistent personal AI.")
    await post_event(client, "I prefer local-first storage over cloud-only solutions.")
    await client.post(
        "/v1/focus",
        json={"label": "Ship EV memory engine", "kind": "goal"},
    )

    resp = await client.get("/v1/ops/center")
    assert resp.status_code == 200, resp.text
    ops = resp.json()
    assert ops["focus"]["label"] == "Ship EV memory engine"
    assert ops["state"]["open_decisions"]
    assert ops["next_actions"]
    assert ops["fleet"] is not None

    resp = await client.get("/v1/twin")
    assert resp.status_code == 200
    twin = resp.json()
    assert twin["goals"]
    assert twin["preferences"]
    assert twin["confidence"] > 0
