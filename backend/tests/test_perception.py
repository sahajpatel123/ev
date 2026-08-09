"""Perception tests: audio scene understanding, location/presence context, privacy boundary.

Covers the permissioned observation layer: collectors emit raw-ish events, EV
derives minimal representations (scene/call state, coarse place/presence), and
the model-facing slice never carries raw transcripts or exact coordinates.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.live import query_live_events
from app.ev.user_state import build_user_state


async def test_audio_scene_context_and_signal_exclude_transcript(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    resp = await client.post(
        "/v1/live/events",
        json={
            "channel": "audio-ambient",
            "kind": "audio",
            "events": [
                {
                    "event_type": "scene",
                    "payload": {
                        "scene": "meeting",
                        "confidence": 0.93,
                        "in_call": True,
                        "transcript": "quarterly results are confidential",
                        "text": "quarterly results are confidential",
                    },
                }
            ],
        },
    )
    assert resp.status_code == 201, resp.text

    state = (await client.get("/v1/state")).json()
    audio_lines = [line for line in state["live_context"] if "audio-ambient" in line]
    assert audio_lines, state["live_context"]
    assert "scene=meeting" in audio_lines[0]
    assert "in_call=True" in audio_lines[0]
    assert "quarterly results are confidential" not in audio_lines[0]

    resp = await client.post("/v1/sense/predict", json={"window_days": 30})
    assert resp.status_code == 200, resp.text
    predictions = {p["kind"]: p for p in resp.json()["predictions"]}
    assert "audio_in_call" in predictions
    signal = predictions["audio_in_call"]
    assert signal["basis_ids"]
    assert "audio" in signal["why_now"].lower()

    # The model-facing derived slice is the one the context compiler uses; it
    # must never contain the raw transcript even though the event is stored.
    model_state = await build_user_state(db_session, access="model")
    model_lines = model_state.live_context
    assert any("scene=meeting" in line and "in_call=True" in line for line in model_lines)
    assert not any("quarterly results are confidential" in line for line in model_lines)


async def test_location_context_and_signal_exclude_coordinates(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    resp = await client.post(
        "/v1/live/events",
        json={
            "channel": "location-coarse",
            "kind": "location",
            "events": [
                {
                    "event_type": "location_change",
                    "payload": {
                        "place": "Bengaluru Airport",
                        "presence": "present",
                        "latitude": 12.99,
                        "longitude": 77.6,
                        "text": "12.99, 77.6",
                    },
                }
            ],
        },
    )
    assert resp.status_code == 201, resp.text

    state = (await client.get("/v1/state")).json()
    location_lines = [line for line in state["live_context"] if "location-coarse" in line]
    assert location_lines, state["live_context"]
    assert "Bengaluru Airport" in location_lines[0]
    assert "presence=present" in location_lines[0]
    assert "12.99" not in location_lines[0]
    assert "77.6" not in location_lines[0]

    resp = await client.post("/v1/sense/predict", json={"window_days": 30})
    assert resp.status_code == 200, resp.text
    predictions = {p["kind"]: p for p in resp.json()["predictions"]}
    assert "location_presence" in predictions
    assert predictions["location_presence"]["basis_ids"]

    model_state = await build_user_state(db_session, access="model")
    model_lines = model_state.live_context
    assert any("Bengaluru Airport" in line for line in model_lines)
    assert not any("12.99" in line or "77.6" in line for line in model_lines)


async def test_sensitive_audio_channel_excluded_from_model_state(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    resp = await client.post(
        "/v1/live/channels",
        json={
            "name": "private-audio",
            "kind": "audio",
            "privacy_level": "sensitive",
            "metadata": {"collector": "mac-mic"},
        },
    )
    assert resp.status_code == 201, resp.text
    channel_id = resp.json()["id"]

    resp = await client.post(
        f"/v1/live/channels/{channel_id}/events",
        json=[
            {
                "event_type": "scene",
                "payload": {"scene": "meeting", "in_call": True, "transcript": "private strategy"},
            }
        ],
    )
    assert resp.status_code == 201, resp.text

    user_state = await build_user_state(db_session, access="user")
    assert any("private-audio" in line for line in user_state.live_context)
    assert not any("private strategy" in line for line in user_state.live_context)

    model_state = await build_user_state(db_session, access="model")
    assert not any("private-audio" in line for line in model_state.live_context)
    model_rows = await query_live_events(db_session, access="model")
    assert model_rows == []


async def test_music_scene_does_not_trigger_in_call_signal(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/live/events",
        json={
            "channel": "audio-music",
            "kind": "audio",
            "events": [
                {
                    "event_type": "scene",
                    "payload": {"scene": "music", "music": True, "confidence": 0.88},
                }
            ],
        },
    )
    assert resp.status_code == 201, resp.text

    state = (await client.get("/v1/state")).json()
    audio_lines = [line for line in state["live_context"] if "audio-music" in line]
    assert audio_lines
    assert "scene=music" in audio_lines[0]
    assert "in_call=False" in audio_lines[0]

    resp = await client.post("/v1/sense/predict", json={"window_days": 30})
    assert resp.status_code == 200, resp.text
    predictions = {p["kind"] for p in resp.json()["predictions"]}
    assert "audio_in_call" not in predictions


async def test_perception_signals_use_recent_permissioned_events_only(
    client: AsyncClient,
) -> None:
    old = (datetime.now(UTC) - timedelta(days=20)).isoformat()
    resp = await client.post(
        "/v1/live/events",
        json={
            "channel": "audio-old",
            "kind": "audio",
            "events": [
                {
                    "event_type": "scene",
                    "payload": {"scene": "meeting", "in_call": True},
                    "occurred_at": old,
                }
            ],
        },
    )
    assert resp.status_code == 201, resp.text

    resp = await client.post("/v1/sense/predict", json={"window_days": 30})
    assert resp.status_code == 200, resp.text
    predictions = {p["kind"]: p for p in resp.json()["predictions"]}
    assert "audio_in_call" in predictions  # 20 days ago is inside the 30-day window

    resp = await client.post("/v1/sense/predict", json={"window_days": 7})
    assert resp.status_code == 200, resp.text
    predictions = {p["kind"] for p in resp.json()["predictions"]}
    assert "audio_in_call" not in predictions
