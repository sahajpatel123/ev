"""Provenance chips: memory+event citation, owner-row chips, audit surface."""

from __future__ import annotations

from uuid import uuid4

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.ev import owner_model as owner_store
from app.filter.envelope import Claim, GroundingMaterial
from app.filter.output_filter import audit_grounding, enforce_provenance_chips
from app.memory.owner_relevance import owner_grounding_materials
from app.models import Event
from app.schemas import EventCreate
from app.services.event_service import EventService


async def _seed_event(db_session: AsyncSession, text: str = "I always write tests first.") -> Event:
    event = await EventService(db_session, actor="test").create(
        EventCreate(source="test", event_type="note", text=text)
    )
    await db_session.commit()
    return event


async def _grant_consent(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/training/consent", json={"track": "life_data_personalization"}
    )
    assert resp.status_code == 201, resp.text


def _claim(text: str, evidence: list[str]) -> Claim:
    return Claim(
        text=text, kind="personal", supported=True, evidence=evidence, score=1.0, action="keep"
    )


def test_memory_chip_cites_memory_and_event() -> None:
    material = [
        GroundingMaterial(
            text="You prefer tea every morning",
            memory_id="m-kyoto",
            memory_type="preference",
            source_event_ids=["e-1", "e-2"],
        )
    ]
    draft = "You prefer tea every morning."
    final, chips, _ = enforce_provenance_chips(
        draft, material, [_claim("You prefer tea every morning", ["m-kyoto"])]
    )
    assert "source: your memory m-kyoto" in final
    assert "← event e-1" in final
    assert chips[0]["memory_id"] == "m-kyoto"
    assert chips[0]["event_id"] == "e-1"


def test_memory_chip_without_events_matches_legacy_shape() -> None:
    material = [
        GroundingMaterial(
            text="You prefer tea",
            memory_id="m-plain",
            memory_type="preference",
        )
    ]
    draft = "You prefer tea."
    final, chips, _ = enforce_provenance_chips(
        draft, material, [_claim("You prefer tea", ["m-plain"])]
    )
    assert final == 'You prefer tea. (source: your memory m-plain · “You prefer tea”)'
    assert chips[0] == {"memory_id": "m-plain", "chip": chips[0]["chip"], "event_id": None}


def test_owner_chip_cites_kind_row_and_event() -> None:
    row_id = str(uuid4())
    material = [
        GroundingMaterial(
            text="concise summaries",
            memory_id=f"owner:trait:{row_id}",
            memory_type="owner_trait",
            source_event_ids=["e-9"],
        )
    ]
    draft = "You write concise summaries."
    final, chips, _ = enforce_provenance_chips(
        draft, material, [_claim("concise summaries", [f"owner:trait:{row_id}"])]
    )
    assert f"source: your owner trait {row_id}" in final
    assert "← event e-9" in final
    assert chips[0]["owner_kind"] == "trait"
    assert chips[0]["owner_row_id"] == row_id
    assert chips[0]["event_id"] == "e-9"


def test_chips_disabled_restores_legacy_shape(monkeypatch) -> None:
    monkeypatch.setattr(settings, "owner_chips_enabled", False)
    material = [
        GroundingMaterial(
            text="You prefer tea",
            memory_id="m-plain",
            memory_type="preference",
            source_event_ids=["e-1"],
        )
    ]
    draft = "You prefer tea."
    final, chips, _ = enforce_provenance_chips(
        draft, material, [_claim("You prefer tea", ["m-plain"])]
    )
    assert final == 'You prefer tea. (source: your memory m-plain · “You prefer tea”)'
    assert chips[0] == {"memory_id": "m-plain", "chip": chips[0]["chip"]}


def test_unsupported_claim_removed() -> None:
    material = [
        GroundingMaterial(
            text="Unrelated apple banana chair",
            memory_id="m-other",
            memory_type="fact",
        )
    ]
    claims, flags = audit_grounding("Your deadline is Friday.", material)
    assert claims
    assert all(c.action == "remove" for c in claims)
    assert any(f.name == "ungrounded_claims_removed" for f in flags)


def test_supported_claim_kept_with_evidence() -> None:
    material = [
        GroundingMaterial(
            text="Your deadline is Friday for the quarterly report",
            memory_id="m-due",
            memory_type="fact",
        )
    ]
    claims, _ = audit_grounding("Your deadline is Friday.", material)
    assert claims
    assert all(c.action == "keep" for c in claims)
    assert claims[0].evidence == ["m-due"]


async def test_owner_grounding_materials_gated(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    event = await _seed_event(db_session)
    await owner_store.write_owner_row(
        db_session, "trait", "concise summaries", source_event_id=event.id
    )
    await db_session.commit()
    assert await owner_grounding_materials(db_session) == []
    monkeypatch.setattr(settings, "owner_model_enabled", True)
    assert await owner_grounding_materials(db_session) == []
    await _grant_consent(client)
    materials = await owner_grounding_materials(db_session)
    assert len(materials) == 1
    assert materials[0].memory_id.startswith("owner:trait:")
    assert materials[0].source_event_ids == [str(event.id)]


async def test_audit_endpoint_shows_version_chain(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "owner_model_enabled", True)
    event = await _seed_event(db_session)
    row = await owner_store.write_owner_row(
        db_session, "value", "Craft over speed", source_event_id=event.id
    )
    await db_session.commit()

    resp = await client.post(
        "/v1/owner/correction",
        json={
            "row_id": str(row.id),
            "kind": "value",
            "corrected_text": "Craft over speed, always",
            "reason": "owner correction",
        },
    )
    assert resp.status_code == 201, resp.text
    corrected_id = resp.json()["id"]

    resp = await client.get(f"/v1/owner/model/{corrected_id}/audit", params={"kind": "value"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "value"
    assert [v["version"] for v in body["versions"]] == [1, 2]
    assert body["versions"][0]["is_current"] is False
    assert body["versions"][0]["superseded_by_id"] == corrected_id
    assert body["versions"][1]["is_current"] is True
    assert body["versions"][1]["confidence"] == 1.0
    # Both versions carry resolvable source events.
    assert body["versions"][0]["source_events"]
    assert len(body["versions"][1]["source_events"]) == 2


async def test_audit_endpoint_404s(client: AsyncClient, monkeypatch) -> None:
    resp = await client.get(f"/v1/owner/model/{uuid4()}/audit", params={"kind": "trait"})
    assert resp.status_code == 404
    monkeypatch.setattr(settings, "owner_model_enabled", True)
    resp = await client.get(f"/v1/owner/model/{uuid4()}/audit", params={"kind": "trait"})
    assert resp.status_code == 404
