"""Owner state guidance (no-moralize) + home-station owner sync."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import uuid4

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.ev import owner_model as owner_store
from app.ev.interaction import (
    OWNER_STATE_GUIDANCE,
    build_strategy,
    strategy_block,
)
from app.ev.user_state import (
    maybe_record_owner_state,
    owner_state_label_for_guidance,
)
from app.models import Event
from app.schemas import EventCreate
from app.services.event_service import EventService
from app.services.processor import ensure_processed

BANNED_GUIDANCE_PHRASES = [
    "you should have",
    "shame on you",
    "what's wrong with you",
    "calm down",
    "just relax",
    "snap out of it",
    "you need therapy",
    "disappointed in you",
    "you always overreact",
    "stop being",
]
DIAGNOSIS_RE = re.compile(
    r"\byou (have|are) (depress|anxie|anxious|adhd|bipolar|ocd|ptsd|burnout)\b",
    re.IGNORECASE,
)


async def _seed_event(
    db_session: AsyncSession, text: str, event_type: str = "message.user"
) -> Event:
    event = await EventService(db_session, actor="test").create(
        EventCreate(source="test", event_type=event_type, text=text)
    )
    await db_session.commit()
    return event


async def _grant_consent(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/training/consent", json={"track": "life_data_personalization"}
    )
    assert resp.status_code == 201, resp.text


async def test_state_builder_disabled_by_default(db_session: AsyncSession) -> None:
    assert settings.owner_state_enabled is False
    event = await _seed_event(db_session, "I'm so stressed about this deadline.")
    assert await maybe_record_owner_state(db_session, event) is None


async def test_state_builder_ignores_non_owner_text(db_session: AsyncSession, monkeypatch) -> None:
    monkeypatch.setattr(settings, "owner_state_enabled", True)
    note = await _seed_event(db_session, "I'm so stressed about this deadline.", "note")
    assert await maybe_record_owner_state(db_session, note) is None
    assistant = await _seed_event(
        db_session, "I'm so stressed about this deadline.", "message.assistant"
    )
    assert await maybe_record_owner_state(db_session, assistant) is None


async def test_state_builder_requires_consent(
    db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "owner_state_enabled", True)
    event = await _seed_event(db_session, "I'm so stressed about this deadline.")
    assert await maybe_record_owner_state(db_session, event) is None


async def test_state_builder_ignores_neutral_text(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "owner_state_enabled", True)
    await _grant_consent(client)
    event = await _seed_event(db_session, "What time is my meeting tomorrow?")
    assert await maybe_record_owner_state(db_session, event) is None


async def test_state_builder_records_affect_snapshot(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "owner_state_enabled", True)
    await _grant_consent(client)
    event = await _seed_event(db_session, "I'm so stressed about this deadline.")
    snapshot = await maybe_record_owner_state(db_session, event)
    assert snapshot is not None
    assert snapshot.state_kind == "affect"
    assert snapshot.label == "stressed"
    assert snapshot.confidence == 0.6
    assert snapshot.details["source_event_id"] == str(event.id)
    assert snapshot.expires_at > datetime.now(UTC)


async def test_state_records_through_processing(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "owner_state_enabled", True)
    await _grant_consent(client)
    event = await _seed_event(db_session, "Ugh, this bug is so frustrating.")
    await ensure_processed(event.id)
    assert await owner_state_label_for_guidance(db_session) == "frustrated"


async def test_revoked_consent_hides_guidance_label(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "owner_state_enabled", True)
    await _grant_consent(client)
    event = await _seed_event(db_session, "I'm exhausted and wiped out.")
    await maybe_record_owner_state(db_session, event)
    await db_session.commit()
    assert await owner_state_label_for_guidance(db_session) == "tired"
    resp = await client.post(
        "/v1/training/consent/life_data_personalization/revoke",
        json={"reason": "privacy review"},
    )
    assert resp.status_code == 200, resp.text
    assert await owner_state_label_for_guidance(db_session) is None


def test_strategy_block_echoes_recent_state_only_in_silence() -> None:
    neutral = build_strategy("What time is my meeting tomorrow?")
    assert neutral.emotional_state in (None, "neutral")
    block = strategy_block(neutral, owner_state="stressed")
    assert "Owner state:" in block
    assert "No pep talk." in block

    live = build_strategy("I'm furious about this ridiculous bug, ugh.")
    assert live.emotional_state == "frustrated"
    block_live = strategy_block(live, owner_state="stressed")
    assert "Owner emotion: frustrated" in block_live
    assert "Owner state:" not in block_live


def test_guidance_never_moralizes_or_diagnoses() -> None:
    messages = [
        "I'm so stressed about this deadline.",
        "I'm furious about this ridiculous bug, ugh.",
        "I'm exhausted and wiped out, need sleep.",
        "Feeling really down and lonely tonight.",
        "I'm so excited and pumped for launch!",
        "What time is my meeting tomorrow?",
    ]
    labels = [None, *OWNER_STATE_GUIDANCE, "unknown-label"]
    for message in messages:
        strategy = build_strategy(message)
        for label in labels:
            block = strategy_block(strategy, owner_state=label)
            lowered = block.lower()
            for phrase in BANNED_GUIDANCE_PHRASES:
                assert phrase not in lowered, (message, label, phrase)
            assert DIAGNOSIS_RE.search(block) is None, (message, label)


async def _seed_owner_row(db_session: AsyncSession, kind: str, text: str):
    event = await _seed_event(db_session, "seed")
    row = await owner_store.write_owner_row(
        db_session, kind, text, source_event_id=event.id
    )
    await db_session.commit()
    return row


async def test_owner_changes_serves_rows_to_master(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _seed_owner_row(db_session, "trait", "concise summaries")
    await _seed_owner_row(db_session, "value", "craft over speed")
    resp = await client.get("/v1/everywhere/owner/changes")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["count"] == 2
    assert {r["kind"] for r in body["rows"]} == {"trait", "value"}
    assert all(r["source_event_ids"] for r in body["rows"])
    assert all("state" not in r for r in body["rows"])
    assert body["next_cursor"].startswith("owner-v1|")


async def test_owner_changes_pages_with_cursor(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _seed_owner_row(db_session, "trait", "first trait here")
    await _seed_owner_row(db_session, "trait", "second trait here")
    first = await client.get("/v1/everywhere/owner/changes", params={"limit": 1})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["count"] == 1
    assert body["has_more"] is True
    second = await client.get(
        "/v1/everywhere/owner/changes",
        params={"limit": 1, "cursor": body["next_cursor"]},
    )
    assert second.status_code == 200, second.text
    body2 = second.json()
    assert body2["count"] == 1
    assert body2["rows"][0]["id"] != body["rows"][0]["id"]
    assert body2["has_more"] is False


async def test_owner_changes_rejects_foreign_epoch(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _seed_owner_row(db_session, "trait", "concise summaries")
    bad_cursor = f"owner-v1|foreign-epoch|{datetime.now(UTC).isoformat()}|{uuid4()}"
    resp = await client.get("/v1/everywhere/owner/changes", params={"cursor": bad_cursor})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["error"] == "STATE_EPOCH_MISMATCH"
    assert body["reset_required"] is True


async def test_owner_changes_rejects_garbage_cursor(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    resp = await client.get("/v1/everywhere/owner/changes", params={"cursor": "nope"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["error"] == "CURSOR_INVALID"


async def test_owner_changes_forbids_sandbox_phone(gateway_phone) -> None:
    _, phone = gateway_phone
    resp = await phone.get("/v1/everywhere/owner/changes")
    assert resp.status_code == 403, resp.text
    assert resp.headers.get("X-Error-Code") == "owner_trust_required"


async def test_bootstrap_includes_owner_rows_only_when_trusted(
    client: AsyncClient, db_session: AsyncSession, gateway_phone
) -> None:
    await _seed_owner_row(db_session, "trait", "concise summaries")
    await owner_store.record_owner_state(
        db_session, state_kind="affect", label="stressed", ttl_s=3600
    )
    await db_session.commit()

    master = await client.get("/v1/everywhere/bootstrap")
    assert master.status_code == 200, master.text
    owner_rows = master.json()["owner_model"]
    assert len(owner_rows) == 1
    assert owner_rows[0]["text"] == "concise summaries"
    assert "state" not in master.json()["owner_model"][0]

    _, phone = gateway_phone
    sandbox = await phone.get("/v1/everywhere/bootstrap")
    assert sandbox.status_code == 200, sandbox.text
    assert sandbox.json()["owner_model"] == []


async def test_forgotten_rows_sync_as_hidden(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    row = await _seed_owner_row(db_session, "trait", "night owl hours")
    await owner_store.forget_owner_row(db_session, "trait", row.id, actor="test")
    await db_session.commit()
    resp = await client.get("/v1/everywhere/owner/changes")
    assert resp.status_code == 200, resp.text
    rows = resp.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["is_current"] is False
    assert rows[0]["forgotten"] is True
