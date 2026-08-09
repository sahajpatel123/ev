"""Perception tests: audio scene understanding, location/presence context, privacy boundary.

Covers the permissioned observation layer: collectors emit raw-ish events, EV
derives minimal representations (scene/call state, coarse place/presence), and
the model-facing slice never carries raw transcripts or exact coordinates.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import ChatMessage, ChatResult, MediaPart, RequestEnvelope
from app.ev import vision
from app.ev.live import query_live_events
from app.ev.user_state import build_user_state
from app.gateway.providers import DeepSeekProvider, MockProvider
from app.gateway.service import ModelGateway


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


class FakeVisionProvider:
    """Deterministic vision-capable provider used only in tests."""

    name = "fake-vision"
    supports_media = True

    def __init__(self) -> None:
        self.seen_messages = []

    async def chat(
        self,
        messages,
        *,
        model=None,
        temperature=0.7,
    ) -> ChatResult:
        self.seen_messages.extend(messages)
        return ChatResult(
            text=(
                "SUMMARY: A person at a workbench with a laptop.\n"
                "LABEL: workbench 0.92\n"
                "LABEL: Maya 0.88"
            ),
            usage={},
            model="fake-vision-model",
        )

    async def chat_with_tools(
        self,
        messages,
        tools,
        *,
        model=None,
        temperature=0.7,
    ) -> ChatResult:
        return await self.chat(messages, model=model, temperature=temperature)

    async def list_models(self) -> list[str]:
        return ["fake-vision-model"]


async def upload_attachment(
    client: AsyncClient,
    *,
    content: bytes = b"fake-image-bytes",
    content_type: str = "image/png",
    filename: str = "photo.png",
    metadata: dict | None = None,
    privacy_level: str = "normal",
) -> str:
    resp = await client.post(
        "/v1/attachments",
        files={"file": (filename, content, content_type)},
        data={
            "metadata": json.dumps(metadata or {}),
            "privacy_level": privacy_level,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["attachment"]["id"]


async def test_vision_analyze_requires_permission(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    attachment_id = await upload_attachment(client)
    with pytest.raises(PermissionError):
        await vision.analyze_attachment(
            db_session,
            UUID(attachment_id),
            actor="master",
            permission=False,
            provider=FakeVisionProvider(),
        )

    resp = await client.post(
        "/v1/vision/analyze",
        json={"attachment_id": attachment_id, "permission": False},
    )
    assert resp.status_code == 403, resp.text
    assert "permission" in resp.json()["detail"].lower()

    resp = await client.get("/v1/vision/perceptions")
    assert resp.json() == []


async def test_vision_analyze_derived_text_never_sends_raw(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    attachment_id = await upload_attachment(
        client,
        metadata={"derived_text": "Invoice EV-42 total 100 USD"},
    )
    provider = FakeVisionProvider()
    row = await vision.analyze_attachment(
        db_session,
        UUID(attachment_id),
        actor="master",
        permission=True,
        allow_raw=False,
        provider=provider,
    )
    await db_session.commit()

    payload = row.payload
    assert payload["raw_sent"] is False
    assert payload["derived_text_used"] is True
    assert payload["provider"] == "fake-vision"
    assert payload["source_event_id"]
    assert payload["request_id"]
    assert "workbench" in payload["summary"].lower()
    assert "data:image" not in json.dumps(payload)

    assert len(provider.seen_messages) == 2
    media = provider.seen_messages[1].media
    assert len(media) == 1
    assert media[0].data_url is None
    assert "Invoice EV-42" in (media[0].text or "")
    assert media[0].ref == attachment_id

    # Suggestions are pending (source=model), not durable identity yet.
    recognitions = (
        await client.get("/v1/vision/log")
    ).json()
    suggestions = [r for r in recognitions if r["source"] == "model"]
    assert {r["label"] for r in suggestions} >= {"workbench", "Maya"}
    assert all(r["entity_id"] is None for r in suggestions)


async def test_vision_analyze_raw_sends_data_url_when_permitted(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    attachment_id = await upload_attachment(client)
    provider = FakeVisionProvider()
    row = await vision.analyze_attachment(
        db_session,
        UUID(attachment_id),
        actor="master",
        permission=True,
        allow_raw=True,
        provider=provider,
    )
    await db_session.commit()

    assert row.payload["raw_sent"] is True
    assert row.payload["content_type"] == "image/png"
    assert row.payload["request_id"]
    # The raw bytes are transmitted to the provider but never persisted in the
    # perception record itself (only provenance hashes are kept).
    assert "data:image" not in json.dumps(row.payload)
    media = provider.seen_messages[1].media
    assert media[0].data_url and media[0].data_url.startswith("data:image/png;base64,")
    assert media[0].sha256


async def test_vision_analyze_blocks_raw_for_sensitive_source(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    attachment_id = await upload_attachment(
        client,
        metadata={"derived_text": "private strategy notes"},
        privacy_level="sensitive",
    )
    provider = FakeVisionProvider()
    row = await vision.analyze_attachment(
        db_session,
        UUID(attachment_id),
        actor="master",
        permission=True,
        allow_raw=True,
        provider=provider,
    )
    await db_session.commit()

    # Fail closed: no provider call at all for sensitive sources.
    assert provider.seen_messages == []
    assert row.payload["raw_sent"] is False
    assert "blocked" in row.payload["summary"].lower()
    assert row.privacy_level == "sensitive"
    assert (await client.get("/v1/vision/log")).json() == []


async def test_vision_confirm_promotes_model_suggestion(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    attachment_id = await upload_attachment(
        client,
        metadata={"derived_text": "Maya at the workbench"},
    )
    await vision.analyze_attachment(
        db_session,
        UUID(attachment_id),
        actor="master",
        permission=True,
        allow_raw=False,
        provider=FakeVisionProvider(),
    )
    await db_session.commit()

    suggestions = [
        r for r in (await client.get("/v1/vision/log")).json() if r["source"] == "model"
    ]
    target = next(r for r in suggestions if r["label"] == "Maya")
    resp = await client.post(
        f"/v1/vision/recognitions/{target['id']}/confirm",
        json={"entity_type": "person"},
    )
    assert resp.status_code == 200, resp.text
    confirmed = resp.json()
    assert confirmed["source"] == "user"
    assert confirmed["entity_id"] is not None

    resp = await client.get("/v1/people/Maya/whereabouts")
    assert resp.status_code == 200
    assert resp.json()["entity_id"] == confirmed["entity_id"]

    # Confirming again is idempotent.
    resp = await client.post(
        f"/v1/vision/recognitions/{target['id']}/confirm",
        json={"entity_type": "person"},
    )
    assert resp.status_code == 200
    assert resp.json()["source"] == "user"

    # Confirmation becomes durable, queryable memory with provenance.
    resp = await client.get("/v1/memories?memory_type=observation")
    assert resp.status_code == 200, resp.text
    memories = [
        m for m in resp.json()["memories"] if m["payload"].get("recognition_id") == target["id"]
    ]
    assert memories, resp.json()
    memory = memories[0]
    assert memory["memory_type"] == "observation"
    assert memory["source_type"] == "explicit"
    assert "Maya" in memory["text"]
    assert memory["entities"]

    audit = (await client.get(f"/v1/audit/{memory['id']}")).json()
    assert audit["memory"]["id"] == memory["id"]
    assert audit["source_events"]
    assert any(e["event_type"] == "recognition.confirm" for e in audit["source_events"])


async def test_vision_perception_context_flows_into_state(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    attachment_id = await upload_attachment(
        client,
        metadata={"derived_text": "workbench with laptop"},
    )
    await vision.analyze_attachment(
        db_session,
        UUID(attachment_id),
        actor="master",
        permission=True,
        allow_raw=False,
        provider=FakeVisionProvider(),
    )
    await db_session.commit()

    state = (await client.get("/v1/state")).json()
    vision_lines = [line for line in state["live_context"] if "vision" in line.lower()]
    assert vision_lines
    assert "workbench" in vision_lines[0].lower()

    perceptions = (await client.get("/v1/vision/perceptions")).json()
    assert len(perceptions) == 1
    assert perceptions[0]["raw_sent"] is False
    assert perceptions[0]["permission_granted_by"] == "master"
    assert perceptions[0]["labels"]

    model_state = await build_user_state(db_session, access="model")
    assert any("workbench" in line.lower() for line in model_state.live_context)


async def test_vision_never_sends_raw_to_text_only_provider(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    attachment_id = await upload_attachment(client)
    provider = MockProvider()
    row = await vision.analyze_attachment(
        db_session,
        UUID(attachment_id),
        actor="master",
        permission=True,
        allow_raw=True,
        provider=provider,
    )
    await db_session.commit()

    assert row.payload["raw_sent"] is False
    assert "data:image" not in json.dumps(row.payload)
    assert row.payload["summary"].startswith("Attachment metadata only")


def test_deepseek_provider_renders_multimodal_content_parts() -> None:
    provider = DeepSeekProvider(
        base_url="http://localhost:0",
        api_key=None,
        default_model="deepseek-v4",
    )

    image = ChatMessage(
        role="user",
        content="Describe this",
        media=[
            MediaPart(
                kind="image",
                content_type="image/png",
                data_url="data:image/png;base64,AAAA",
                ref="att-1",
                sha256="abc",
            )
        ],
    )
    payload = provider._message_payload(image)
    assert payload["content"][0] == {"type": "text", "text": "Describe this"}
    assert payload["content"][1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,AAAA"},
    }

    audio = ChatMessage(
        role="user",
        content="",
        media=[
            MediaPart(
                kind="audio",
                content_type="audio/mpeg",
                data_url="data:audio/mpeg;base64,BBBB",
            )
        ],
    )
    audio_payload = provider._message_payload(audio)
    assert audio_payload["content"][0]["input_audio"] == {
        "data": "BBBB",
        "format": "mp3",
    }

    plain = ChatMessage(role="user", content="plain text")
    assert provider._message_payload(plain) == {"role": "user", "content": "plain text"}


async def test_vision_confirm_memory_survives_rebuild(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    attachment_id = await upload_attachment(
        client,
        metadata={"derived_text": "workbench"},
    )
    await vision.analyze_attachment(
        db_session,
        UUID(attachment_id),
        actor="master",
        permission=True,
        allow_raw=False,
        provider=FakeVisionProvider(),
    )
    await db_session.commit()

    suggestions = [
        r for r in (await client.get("/v1/vision/log")).json() if r["source"] == "model"
    ]
    target = next(r for r in suggestions if r["label"] == "workbench")
    resp = await client.post(
        f"/v1/vision/recognitions/{target['id']}/confirm",
        json={"entity_type": "thing"},
    )
    assert resp.status_code == 200, resp.text

    resp = await client.post("/v1/memory/rebuild")
    assert resp.status_code == 200, resp.text
    assert resp.json()["events_replayed"] > 0

    # The confirmed recognition is re-derived from its raw event after rebuild.
    resp = await client.get("/v1/memories?memory_type=observation")
    assert resp.status_code == 200, resp.text
    memories = [
        m for m in resp.json()["memories"] if m["payload"].get("recognition_id") == target["id"]
    ]
    assert memories, resp.json()
    assert "workbench" in memories[0]["text"]
    assert memories[0]["entities"]

    # Recognition remains user-confirmed and entity-linked after rebuild.
    resp = await client.get("/v1/vision/log")
    confirmed = next(r for r in resp.json() if r["id"] == target["id"])
    assert confirmed["source"] == "user"
    assert confirmed["entity_id"] is not None


async def test_route_briefing_includes_permissioned_location_context(
    client: AsyncClient,
) -> None:
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    await client.post(
        "/v1/alerts/watchlist",
        json={
            "kind": "deadline",
            "value": "Airport pickup",
            "priority": 0.7,
            "metadata": {
                "date": tomorrow,
                "location": "Airport",
                "travel_minutes": 45,
                "prep": "Bring charger",
            },
        },
    )
    await client.post(
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

    resp = await client.get("/v1/hud/route")
    assert resp.status_code == 200, resp.text
    route = resp.json()
    assert route["destination"] == "Airport"
    context_notes = [n for n in route["notes"] if "Live context" in n]
    assert context_notes, route["notes"]
    assert "Bengaluru Airport" in context_notes[0]
    assert "12.99" not in context_notes[0]
    assert "77.6" not in context_notes[0]


def test_request_envelope_audits_media_refs() -> None:
    envelope = RequestEnvelope(
        request_id="req-1",
        strategy={"mode": "perception"},
        media_refs=[
            {
                "kind": "text",
                "content_type": "text/plain",
                "ref": "att-1",
                "sha256": "abc",
                "raw": False,
                "derived_text_used": True,
            }
        ],
    )
    dumped = envelope.to_dict()
    assert dumped["media_refs"][0]["ref"] == "att-1"
    assert dumped["media_refs"][0]["raw"] is False


async def test_gateway_preserves_media_and_redacts_media_secrets() -> None:
    provider = FakeVisionProvider()
    gateway = ModelGateway(provider)
    message = ChatMessage(
        role="user",
        content="Analyze",
        media=[
            MediaPart(
                kind="text",
                content_type="text/plain",
                text="api_key=sk-abcdefghijklmnopqrstuvwxyz123456",
                ref="att-1",
            )
        ],
    )
    call = await gateway.chat(
        [message],
        envelope=RequestEnvelope(request_id="req-media", strategy={}),
    )
    assert call.status == "ok"
    seen = provider.seen_messages[0]
    assert len(seen.media) == 1
    assert "[credential redacted]" in seen.media[0].text
    assert "sk-" not in seen.media[0].text
    assert seen.media[0].ref == "att-1"


async def test_gateway_blocks_forbidden_media_text_before_provider() -> None:
    provider = FakeVisionProvider()
    gateway = ModelGateway(provider)
    message = ChatMessage(
        role="user",
        content="Analyze",
        media=[
            MediaPart(
                kind="text",
                text="this is never_send_to_model content",
                ref="att-1",
            )
        ],
    )
    call = await gateway.chat(
        [message],
        envelope=RequestEnvelope(request_id="req-blocked", strategy={}),
    )
    assert call.status == "blocked"
    assert provider.seen_messages == []
