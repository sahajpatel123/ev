"""PEOPLE FROM LIFE: enrollment, resolution, context, roster, forget (no People tab)."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Entity, FaceEnrollment, Integration, Memory, MemoryEntity
from app.people.cli import _crops_from_files
from app.people.face_embed import FaceCrop


def img(seed: str) -> str:
    return base64.b64encode(seed.encode("ascii") * 80).decode("ascii")


def photos_for(seed: str, count: int = 5) -> list[dict]:
    return [
        {
            "image_b64": img(seed),
            "quality": 0.92,
            "confidence": 0.97,
            "source": "photo",
        }
        for _ in range(count)
    ]


async def grant_face_consent(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/training/consent",
        json={"track": "face_enrollment", "purpose": "life enrollment test"},
    )
    assert resp.status_code == 201, resp.text


async def enroll(client: AsyncClient, name: str, seed: str) -> dict:
    resp = await client.post(
        "/v1/people/enrollments",
        json={"person_name": name, "photos": photos_for(seed)},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _add_memory_person(
    db_session: AsyncSession,
    name: str,
    role: str,
) -> Entity:
    entity = Entity(
        name=name,
        entity_type="person",
        canonical_key=f"person:{name.lower()}",
    )
    db_session.add(entity)
    await db_session.flush()
    memory = Memory(
        memory_type="observation",
        text=f"my {role} is {name}",
        importance=0.5,
        confidence=0.9,
        source_type="explicit",
        privacy_level="normal",
        fingerprint="f" * 64,
    )
    db_session.add(memory)
    await db_session.flush()
    db_session.add(MemoryEntity(memory_id=memory.id, entity_id=entity.id, role=role))
    await db_session.commit()
    return entity


async def test_resolve_relationship_role_from_memory(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _add_memory_person(db_session, "Alice", "mom")
    resp = await client.get("/v1/people/Mom/resolve")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["contacts_available"] is False
    candidates = body["candidates"]
    assert any(
        candidate["name"] == "Alice"
        and candidate["relationship"] == "mom"
        and candidate["provenance"] == "memory"
        and candidate["face_enrolled"] is False
        for candidate in candidates
    )


async def test_resolve_marks_enrolled_person_with_consent(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await grant_face_consent(client)
    payload = await enroll(client, "Alex", "alex")
    resp = await client.get("/v1/people/Alex/resolve")
    assert resp.status_code == 200, resp.text
    candidate = next(
        item for item in resp.json()["candidates"] if item["name"] == "Alex"
    )
    assert candidate["provenance"] == "roster"
    assert candidate["face_enrolled"] is True
    assert candidate["consent_id"] == payload["enrollment"]["consent_id"]


async def test_contact_candidate_never_creates_person(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    db_session.add(
        Integration(
            slug="contacts",
            adapter="contacts",
            name="Contacts",
            scopes=["contacts:read"],
            status="active",
            config={"provider": "macos_life"},
            privacy_level="normal",
        )
    )
    await db_session.commit()

    async def fake_execute_action(session, integration_id, action, args, actor):
        return SimpleNamespace(
            result={"contacts": [{"id": "c1", "name": "Mom Contact"}]}
        )

    monkeypatch.setattr(
        "app.integrations.service.execute_action",
        fake_execute_action,
    )
    resp = await client.get("/v1/people/Mom/resolve")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["contacts_available"] is True
    contact = next(
        item for item in body["candidates"] if item["provenance"] == "contact"
    )
    assert contact["name"] == "Mom Contact"
    assert contact["candidate_only"] is True
    assert contact["face_enrolled"] is False
    assert contact["entity_id"] is None

    count = await db_session.scalar(
        select(func.count()).select_from(Entity).where(Entity.name == "Mom Contact")
    )
    assert count == 0  # contact names never silently become people


async def test_person_context_exposes_enrollment_and_consent(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await grant_face_consent(client)
    await enroll(client, "Alice", "alice")
    resp = await client.get("/v1/people/Alice/context")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["match_state"] == "enrolled"
    assert body["enrolled"] is not None
    assert body["consent"]["consent_id"] is not None
    assert body["consent"]["granted_at"] is not None
    assert body["last_seen"] is None  # never fabricated without an observation


async def test_person_context_last_seen_only_from_real_observations(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await grant_face_consent(client)
    await enroll(client, "Alice", "alice")
    resp = await client.post(
        "/v1/people/recognize",
        json={"image_b64": img("alice"), "source": "photo"},
    )
    recognition_id = resp.json()["recognition_id"]
    await client.post(
        f"/v1/people/recognitions/{recognition_id}/confirm",
        json={"reason": "yes"},
    )
    body = (await client.get("/v1/people/Alice/context")).json()
    assert body["last_seen"] is not None
    assert body["last_seen"]["source"] in ("face", "vision")
    assert body["face_sightings"]


async def test_roster_lists_enrolled_and_memory_people(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await grant_face_consent(client)
    await enroll(client, "Alice", "alice")
    await _add_memory_person(db_session, "Bob", "colleague")

    resp = await client.get("/v1/people/roster")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    by_name = {person["name"]: person for person in body["people"]}
    assert body["total"] == 2
    assert by_name["Alice"]["face_enrolled"] is True
    assert by_name["Alice"]["sample_count"] == 5
    assert by_name["Alice"]["consent_id"] is not None
    assert by_name["Bob"]["face_enrolled"] is False
    assert by_name["Bob"]["relationship"] == "colleague"


async def test_forget_invalidates_recognition_identity(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await grant_face_consent(client)
    payload = await enroll(client, "Alice", "alice")
    entity_id = UUID(payload["enrollment"]["entity_id"])
    resp = await client.post(
        "/v1/people/recognize",
        json={"image_b64": img("alice"), "source": "photo"},
    )
    assert resp.json()["recognition_id"] is not None

    resp = await client.delete(f"/v1/people/{entity_id}")
    assert resp.status_code == 200, resp.text
    manifest = resp.json()
    assert manifest["recognition_logs_deleted"] >= 1
    assert manifest["face_samples_deleted"] == 5

    roster = (await client.get("/v1/people/roster")).json()
    assert all(person["name"] != "Alice" for person in roster["people"])

    resolved = (await client.get("/v1/people/Alice/resolve")).json()
    alice = next(
        (item for item in resolved["candidates"] if item["name"] == "Alice"),
        None,
    )
    assert alice is not None
    assert alice["face_enrolled"] is False
    assert alice["consent_id"] is None

    enrollment = (
        await db_session.execute(
            select(FaceEnrollment).where(FaceEnrollment.entity_id == entity_id)
        )
    ).scalar_one()
    assert enrollment.status == "deleted"
    assert enrollment.ciphertext is None


def test_cli_crop_collection_requires_minimum_photos(tmp_path) -> None:
    files = []
    for index in range(5):
        path = tmp_path / f"{index}.png"
        path.write_bytes(f"photo-{index}".encode() * 20)
        files.append(path)
    crops = _crops_from_files(files, 0.9, 0.95)
    assert len(crops) == 5
    assert all(isinstance(crop, FaceCrop) for crop in crops)
    with pytest.raises(SystemExit, match="need >= 5"):
        _crops_from_files(files[:4], 0.9, 0.95)
