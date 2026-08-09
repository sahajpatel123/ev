"""Tests for the versioned adapter registry with eval gates (Training track 7.3)."""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdapterRegistration, ResponseLog


async def grant_adapter_consent(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/training/consent",
        json={"track": "adapter_fine_tuning"},
    )
    assert resp.status_code == 201, resp.text


async def build_corpus(
    client: AsyncClient,
    db_session: AsyncSession,
    *,
    with_correction: bool = True,
) -> int:
    await client.post(
        "/v1/training/consent",
        json={"track": "training_corpus"},
    )
    if with_correction:
        db_session.add(
            ResponseLog(
                request_text="Fix that: I actually prefer PostgreSQL.",
                reply_text="Corrected to PostgreSQL.",
                mode="casual",
                strategy={},
                provenance_ids=[],
                context_tokens=10,
                was_correction=True,
            )
        )
        await db_session.commit()
    resp = await client.post("/v1/training/corpus/build")
    assert resp.status_code == 201, resp.text
    return resp.json()["snapshot"]["version"]


async def test_adapter_register_requires_consent(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    version = await build_corpus(client, db_session)
    resp = await client.post(
        "/v1/training/adapter/register",
        json={"name": "evie-v1-lora", "corpus_version": version},
    )
    assert resp.status_code == 403
    assert "adapter_fine_tuning" in resp.json()["detail"]


async def test_adapter_register_runs_eval_gates_and_versions(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    version = await build_corpus(client, db_session)
    await grant_adapter_consent(client)

    resp = await client.post(
        "/v1/training/adapter/register",
        json={
            "name": "evie-v1-lora",
            "provider": "local-lora",
            "base_model": "deepseek-v4-flash-0731",
            "adapter_ref": "adapters/evie-v1",
            "corpus_version": version,
        },
    )
    assert resp.status_code == 201, resp.text
    first = resp.json()
    assert first["version"] == 1
    assert first["status"] == "approved"
    assert first["is_current"] is False
    assert first["eval_metrics"]["passed"] is True
    assert first["eval_metrics"]["gates"]["corrections_present"] is True

    resp = await client.post(
        "/v1/training/adapter/register",
        json={"name": "evie-v1-lora", "corpus_version": version},
    )
    assert resp.status_code == 201
    second = resp.json()
    assert second["version"] == 2
    assert second["supersedes_id"] == first["id"]

    resp = await client.post(
        "/v1/training/adapter/activate",
        json={"adapter_id": second["id"], "reason": "new voice/style adapter"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_current"] is True
    assert resp.json()["status"] == "active"

    resp = await client.post(
        "/v1/training/adapter/rollback",
        json={"adapter_id": second["id"], "reason": "regression"},
    )
    assert resp.status_code == 200, resp.text
    rolled_back = resp.json()
    assert rolled_back["id"] == first["id"]
    assert rolled_back["is_current"] is True
    assert rolled_back["status"] == "active"

    resp = await client.get("/v1/training/adapter")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


async def test_adapter_register_rejects_empty_corpus(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    version = await build_corpus(client, db_session, with_correction=False)
    await grant_adapter_consent(client)
    resp = await client.post(
        "/v1/training/adapter/register",
        json={"name": "evie-empty-lora", "corpus_version": version},
    )
    assert resp.status_code == 201, resp.text
    payload = resp.json()
    assert payload["status"] == "rejected"
    assert payload["eval_metrics"]["passed"] is False


async def test_adapter_delete_redacts_all(client: AsyncClient, db_session: AsyncSession) -> None:
    version = await build_corpus(client, db_session)
    await grant_adapter_consent(client)
    resp = await client.post(
        "/v1/training/adapter/register",
        json={"name": "evie-delete-lora", "corpus_version": version},
    )
    assert resp.status_code == 201

    resp = await client.post("/v1/training/adapter/delete")
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] == 1

    rows = list((await db_session.execute(select(AdapterRegistration))).scalars().all())
    assert len(rows) == 1
    assert rows[0].redacted is True
    assert rows[0].status == "deleted"
    assert rows[0].eval_metrics == {}
