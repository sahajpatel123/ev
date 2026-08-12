"""Tests for life-data personalization: evidence-backed importance/retrieval learning."""

from __future__ import annotations

from uuid import UUID

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.retrieval import Retriever
from app.models import PersonalizationCalibration, ResponseLog


async def post_event(client: AsyncClient, text: str) -> dict:
    resp = await client.post(
        "/v1/events",
        json={"source": "test", "event_type": "note", "text": text},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def grant_personalization_consent(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/training/consent",
        json={"track": "life_data_personalization"},
    )
    assert resp.status_code == 201, resp.text


async def seed_response_logs(
    db_session: AsyncSession,
    memory_id: UUID,
    *,
    corrected: int = 0,
    useful: int = 0,
    followed: int = 0,
    ignored: int = 0,
    neutral: int = 0,
) -> None:
    rows: list[ResponseLog] = []
    for _ in range(corrected):
        rows.append(
            ResponseLog(
                request_text="test",
                reply_text="test",
                mode="casual",
                strategy={},
                provenance_ids=[str(memory_id)],
                context_tokens=10,
                was_correction=True,
                was_useful=False,
                followed_recommendation=False,
            )
        )
    for _ in range(useful):
        rows.append(
            ResponseLog(
                request_text="test",
                reply_text="test",
                mode="casual",
                strategy={},
                provenance_ids=[str(memory_id)],
                context_tokens=10,
                was_useful=True,
            )
        )
    for _ in range(followed):
        rows.append(
            ResponseLog(
                request_text="test",
                reply_text="test",
                mode="casual",
                strategy={},
                provenance_ids=[str(memory_id)],
                context_tokens=10,
                followed_recommendation=True,
            )
        )
    for _ in range(ignored):
        rows.append(
            ResponseLog(
                request_text="test",
                reply_text="test",
                mode="casual",
                strategy={},
                provenance_ids=[str(memory_id)],
                context_tokens=10,
                followed_recommendation=False,
            )
        )
    for _ in range(neutral):
        rows.append(
            ResponseLog(
                request_text="test",
                reply_text="test",
                mode="casual",
                strategy={},
                provenance_ids=[str(memory_id)],
                context_tokens=10,
            )
        )
    db_session.add_all(rows)
    await db_session.commit()


async def preference_memory_id(client: AsyncClient, db_session: AsyncSession) -> UUID:
    await post_event(client, "I prefer Python for work.")
    resp = await client.get("/v1/memories?memory_type=preference&q=Python")
    assert resp.status_code == 200, resp.text
    memory = resp.json()["memories"][0]
    return UUID(memory["id"])


async def decision_memory_id(client: AsyncClient, db_session: AsyncSession) -> UUID:
    await post_event(client, "I decided to use Postgres for storage.")
    resp = await client.get("/v1/memories?memory_type=decision&q=Postgres")
    assert resp.status_code == 200, resp.text
    memory = resp.json()["memories"][0]
    return UUID(memory["id"])


async def search_components(
    db_session: AsyncSession, query: str = "Postgres"
) -> dict:
    results = await Retriever(db_session).search(query, k=10)
    decision = next(r for r in results if r.memory_type == "decision")
    return decision.components


async def test_personalization_calibrate_requires_consent(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await decision_memory_id(client, db_session)
    resp = await client.post("/v1/training/personalization/calibrate")
    assert resp.status_code == 403
    assert "consent" in resp.json()["detail"].lower()


async def test_personalization_derives_and_applies_importance_learning(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    memory_id = await decision_memory_id(client, db_session)
    await seed_response_logs(
        db_session,
        memory_id,
        corrected=2,
        useful=1,
        neutral=2,
    )
    await grant_personalization_consent(client)

    resp = await client.post("/v1/training/personalization/calibrate")
    assert resp.status_code == 201, resp.text
    payload = resp.json()
    assert payload["applied"] is True
    assert payload["evidence"]["decision"]["rated"] == 3
    assert payload["evidence"]["decision"]["corrected"] == 2
    assert payload["calibration"]["calibrations"]["decision"] == 0.8

    # Retrieval applies the calibrated importance signal transparently.
    components = await search_components(db_session)
    assert components["personalization"] == 0.8
    assert components["importance"] == round(components["importance_base"] * 0.8, 4)


async def test_personalization_versions_rollback_and_revocation(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    memory_id = await decision_memory_id(client, db_session)
    await seed_response_logs(
        db_session,
        memory_id,
        corrected=2,
        useful=1,
        neutral=2,
    )
    await grant_personalization_consent(client)

    resp = await client.post("/v1/training/personalization/calibrate")
    assert resp.status_code == 201
    assert resp.json()["calibration"]["version"] == 1

    # Overwhelming positive evidence washes out the correction penalty in v2.
    await seed_response_logs(db_session, memory_id, useful=8, followed=8)
    resp = await client.post("/v1/training/personalization/calibrate")
    assert resp.status_code == 201
    v2 = resp.json()["calibration"]
    assert v2["version"] == 2
    assert v2["is_current"] is True
    assert v2["calibrations"]["decision"] == 1.0

    resp = await client.get("/v1/training/personalization/history")
    assert resp.status_code == 200
    assert len(resp.json()) == 2

    resp = await client.post(
        "/v1/training/personalization/rollback",
        json={"target_version": 1, "reason": "positive evidence was noise"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == 1
    assert resp.json()["is_current"] is True
    assert resp.json()["calibrations"]["decision"] == 0.8

    components = await search_components(db_session)
    assert components["personalization"] == 0.8

    # Revoking consent disables learned calibration without deleting history.
    resp = await client.post(
        "/v1/training/consent/life_data_personalization/revoke",
        json={"reason": "privacy review"},
    )
    assert resp.status_code == 200
    components = await search_components(db_session)
    assert components["personalization"] == 1.0

    resp = await client.post("/v1/training/personalization/calibrate")
    assert resp.status_code == 403


async def test_personalization_delete_redacts_all_snapshots(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    memory_id = await decision_memory_id(client, db_session)
    await seed_response_logs(db_session, memory_id, corrected=3, neutral=2)
    await grant_personalization_consent(client)
    resp = await client.post("/v1/training/personalization/calibrate")
    assert resp.status_code == 201

    resp = await client.post("/v1/training/personalization/delete")
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] == 1
    assert resp.json()["applied"] is False

    rows = list(
        (await db_session.execute(select(PersonalizationCalibration))).scalars().all()
    )
    assert len(rows) == 1
    assert rows[0].calibrations == {}
    assert rows[0].evidence == {}
    assert rows[0].is_current is False

    # After redaction, retrieval is neutral even with consent re-granted.
    resp = await client.post(
        "/v1/training/consent",
        json={"track": "life_data_personalization"},
    )
    assert resp.status_code == 201
    components = await search_components(db_session)
    assert components["personalization"] == 1.0


async def test_personalization_follow_ignore_learning_is_per_domain(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Recommendation follow-through raises one domain while ignores lower another."""

    decision_id = await decision_memory_id(client, db_session)
    preference_id = await preference_memory_id(client, db_session)

    # Decision domain: users follow recommendations -> importance boost.
    await seed_response_logs(db_session, decision_id, followed=4, neutral=1)
    # Preference domain: recommendations are ignored -> importance penalty.
    await seed_response_logs(db_session, preference_id, followed=1, ignored=4)
    await grant_personalization_consent(client)

    resp = await client.post("/v1/training/personalization/calibrate")
    assert resp.status_code == 201, resp.text
    payload = resp.json()
    assert payload["calibration"]["calibrations"]["decision"] == 1.05
    assert payload["calibration"]["calibrations"]["preference"] == 0.95
    assert payload["evidence"]["decision"]["followed"] == 4
    assert payload["evidence"]["preference"]["ignored"] == 4
