"""Owner model store: versioned traits/values/thinking-style + lifecycle API."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.ev import owner_model as owner_store
from app.models import Event, OwnerModelEvent
from app.schemas import EventCreate
from app.services.event_service import EventService
from app.services.rebuild import rebuild_derived_state


async def _seed_event(db_session: AsyncSession, text: str = "I prefer tea over coffee.") -> Event:
    return await EventService(db_session, actor="test").create(
        EventCreate(source="test", event_type="note", text=text)
    )


async def test_write_owner_row_creates_v1_with_provenance(db_session: AsyncSession) -> None:
    event = await _seed_event(db_session)
    row = await owner_store.write_owner_row(
        db_session, "trait", "Decisive under pressure", source_event_id=event.id
    )
    assert row.version == 1
    assert row.is_current is True
    assert row.fingerprint
    assert await owner_store.row_source_events(db_session, "trait", row.id) == [event.id]


async def test_write_owner_row_dedupes_same_fingerprint(db_session: AsyncSession) -> None:
    event = await _seed_event(db_session)
    first = await owner_store.write_owner_row(
        db_session, "value", "Honesty over comfort", source_event_id=event.id
    )
    second = await owner_store.write_owner_row(
        db_session, "value", "Honesty over comfort", source_event_id=event.id
    )
    assert second.id == first.id
    assert second.version == 1


async def test_correct_owner_row_supersedes_with_confidence_1(db_session: AsyncSession) -> None:
    event = await _seed_event(db_session)
    row = await owner_store.write_owner_row(
        db_session, "thinking_style", "Thinks out loud", source_event_id=event.id
    )
    corrected = await owner_store.correct_owner_row(
        db_session,
        "thinking_style",
        row.id,
        corrected_text="Thinks in writing first",
        reason="owner correction",
        actor="test",
    )
    assert corrected.version == 2
    assert corrected.confidence == 1.0
    assert corrected.source_type == "explicit"
    assert corrected.supersedes_id == row.id
    assert corrected.version_group == row.version_group
    assert corrected.is_current is True
    assert row.is_current is False
    assert row.superseded_by_id == corrected.id
    # Provenance: copied source event + new correction event.
    sources = await owner_store.row_source_events(db_session, "thinking_style", corrected.id)
    assert event.id in sources
    assert len(sources) == 2


async def test_forget_and_restore_owner_row(db_session: AsyncSession) -> None:
    event = await _seed_event(db_session)
    row = await owner_store.write_owner_row(
        db_session, "trait", "Night owl", source_event_id=event.id
    )
    forgotten = await owner_store.forget_owner_row(
        db_session, "trait", row.id, reason="owner requested", actor="test"
    )
    assert forgotten.is_current is False
    assert forgotten.payload["forgotten"] is True
    assert await owner_store.current_owner_rows(db_session, "trait") == []

    restored = await owner_store.restore_owner_row(db_session, "trait", row.id, actor="test")
    assert restored.is_current is True
    assert restored.payload["forgotten"] is False
    current = await owner_store.current_owner_rows(db_session, "trait")
    assert [r.id for r in current] == [row.id]


async def test_rebuild_leaves_owner_rows_untouched(db_session: AsyncSession) -> None:
    event = await _seed_event(db_session)
    row = await owner_store.write_owner_row(
        db_session, "value", "Family first", source_event_id=event.id
    )
    await rebuild_derived_state(db_session, actor="test", reason="phase 1 compat")
    current = await owner_store.current_owner_rows(db_session, "value")
    assert [r.id for r in current] == [row.id]
    assert current[0].is_current is True


async def test_state_snapshot_ttl(db_session: AsyncSession) -> None:
    live = await owner_store.record_owner_state(
        db_session, state_kind="energy", label="focused", ttl_s=3600
    )
    assert await owner_store.current_owner_state(db_session) is not None
    assert (await owner_store.current_owner_state(db_session)).id == live.id

    expired = await owner_store.record_owner_state(
        db_session, state_kind="energy", label="tired", ttl_s=-1
    )
    assert expired.expires_at < datetime.now(UTC)
    # Expired snapshots never surface, even when newer than a live one.
    current = await owner_store.current_owner_state(db_session)
    assert current is not None
    assert current.id == live.id


async def test_owner_model_api_404_when_disabled(client: AsyncClient) -> None:
    assert settings.owner_model_enabled is False
    resp = await client.get("/v1/owner/model")
    assert resp.status_code == 404, resp.text


async def test_owner_model_api_round_trip(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "owner_model_enabled", True)
    event = await _seed_event(db_session)
    row = await owner_store.write_owner_row(
        db_session, "trait", "Decisive under pressure", source_event_id=event.id
    )
    await db_session.commit()

    resp = await client.get("/v1/owner/model")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [t["text"] for t in body["traits"]] == ["Decisive under pressure"]
    assert body["traits"][0]["source_events"]
    assert body["state"] is None

    resp = await client.post(
        "/v1/owner/correction",
        json={
            "row_id": str(row.id),
            "kind": "trait",
            "corrected_text": "Decisive under pressure, reflective after",
            "reason": "owner correction",
        },
    )
    assert resp.status_code == 201, resp.text
    corrected = resp.json()
    assert corrected["version"] == 2
    assert corrected["confidence"] == 1.0

    resp = await client.post(
        "/v1/owner/forget",
        json={"row_id": corrected["id"], "kind": "trait", "reason": "owner requested"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_current"] is False

    resp = await client.get("/v1/owner/model")
    assert resp.status_code == 200, resp.text
    assert resp.json()["traits"] == []

    resp = await client.post(
        "/v1/owner/restore", json={"row_id": corrected["id"], "kind": "trait"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_current"] is True


async def test_owner_model_api_rejects_fixture_phone(gateway_phone, monkeypatch) -> None:
    monkeypatch.setattr(settings, "owner_model_enabled", True)
    _, phone = gateway_phone
    resp = await phone.get("/v1/owner/model")
    assert resp.status_code == 403, resp.text


def test_require_owner_trust_branches() -> None:
    from types import SimpleNamespace

    import pytest
    from fastapi import HTTPException

    from app.api.core import _require_owner_trust

    # Master key always passes.
    _require_owner_trust(SimpleNamespace(is_master=True, device=None))
    # Owner-trusted device passes.
    _require_owner_trust(
        SimpleNamespace(is_master=False, device=SimpleNamespace(trust_level="owner"))
    )
    # Plain device is refused with the owner_trust_required code.
    with pytest.raises(HTTPException) as exc_info:
        _require_owner_trust(
            SimpleNamespace(is_master=False, device=SimpleNamespace(trust_level="device"))
        )
    assert exc_info.value.status_code == 403
    assert exc_info.value.headers == {"X-Error-Code": "owner_trust_required"}
    # No device and no master key is refused too.
    with pytest.raises(HTTPException) as exc_info:
        _require_owner_trust(SimpleNamespace(is_master=False, device=None))
    assert exc_info.value.status_code == 403


async def test_owner_correction_unknown_row_404(client: AsyncClient, monkeypatch) -> None:
    monkeypatch.setattr(settings, "owner_model_enabled", True)
    resp = await client.post(
        "/v1/owner/correction",
        json={
            "row_id": str(uuid4()),
            "kind": "value",
            "corrected_text": "whatever",
            "reason": "owner correction",
        },
    )
    assert resp.status_code == 404, resp.text


async def test_owner_api_includes_state_snapshot(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "owner_model_enabled", True)
    await owner_store.record_owner_state(
        db_session, state_kind="focus", label="deep work", ttl_s=3600
    )
    # An expired snapshot must not leak into the response.
    await owner_store.record_owner_state(
        db_session, state_kind="focus", label="stale", ttl_s=-1
    )
    await db_session.commit()
    resp = await client.get("/v1/owner/model")
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"]["label"] == "deep work"


async def test_owner_provenance_join_links_row_to_event(db_session: AsyncSession) -> None:
    event = await _seed_event(db_session)
    row = await owner_store.write_owner_row(
        db_session, "value", "Craft over speed", source_event_id=event.id
    )
    link = await db_session.get(
        OwnerModelEvent, {"row_kind": "value", "row_id": row.id, "event_id": event.id}
    )
    assert link is not None
    event_back = await db_session.get(Event, link.event_id)
    assert event_back is not None
    assert event_back.id == event.id


async def test_owner_row_expires_excluded_after_ttl(db_session: AsyncSession, monkeypatch) -> None:
    monkeypatch.setattr(settings, "owner_state_ttl_s", 3600)
    snapshot = await owner_store.record_owner_state(
        db_session, state_kind="energy", label="fresh"
    )
    assert snapshot.expires_at - snapshot.created_time == timedelta(seconds=3600)
