"""Tests for filter self-improvement driven by the filter/decision ledger (7.5)."""

from __future__ import annotations

from uuid import uuid4

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FilterLedger, FilterRecalibration, ResponseLog


async def grant_filter_consent(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/training/consent",
        json={"track": "filter_self_improvement"},
    )
    assert resp.status_code == 201, resp.text


async def seed_ledger_and_signals(db_session: AsyncSession) -> None:
    rows = [
        FilterLedger(
            request_id=str(uuid4()),
            stage="pipeline",
            action="run",
            name="intelligence_filter",
            final_text="polished reply",
            iterations=1,
        ),
        FilterLedger(
            request_id=str(uuid4()),
            stage="pipeline",
            action="run",
            name="intelligence_filter",
            final_text="polished reply 2",
            iterations=1,
        ),
        FilterLedger(
            request_id=str(uuid4()),
            stage="pipeline",
            action="run",
            name="intelligence_filter",
            final_text="polished reply 3",
            iterations=1,
        ),
        FilterLedger(
            request_id=str(uuid4()),
            stage="input",
            action="block",
            name="input_filter",
            severity="high",
            final_text="blocked",
        ),
        ResponseLog(
            request_text="That was wrong.",
            reply_text="Sorry, here is the corrected answer.",
            mode="casual",
            strategy={},
            provenance_ids=[],
            context_tokens=10,
            was_correction=True,
        ),
        ResponseLog(
            request_text="Still wrong.",
            reply_text="Corrected again.",
            mode="casual",
            strategy={},
            provenance_ids=[],
            context_tokens=10,
            was_correction=True,
        ),
        ResponseLog(
            request_text="Now it is right.",
            reply_text="Great.",
            mode="casual",
            strategy={},
            provenance_ids=[],
            context_tokens=10,
            was_useful=True,
        ),
        ResponseLog(
            request_text="Not what I asked.",
            reply_text="Sorry.",
            mode="casual",
            strategy={},
            provenance_ids=[],
            context_tokens=10,
            was_useful=False,
        ),
        ResponseLog(
            request_text="Too verbose.",
            reply_text="Shortened.",
            mode="casual",
            strategy={},
            provenance_ids=[],
            context_tokens=10,
            was_useful=False,
        ),
    ]
    db_session.add_all(rows)
    await db_session.commit()


async def test_filter_self_improve_requires_consent(client: AsyncClient) -> None:
    resp = await client.post("/v1/training/filter/self-improve")
    assert resp.status_code == 403
    assert "filter_self_improvement" in resp.json()["detail"]


async def test_filter_self_improve_derives_proposals(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await seed_ledger_and_signals(db_session)
    await grant_filter_consent(client)

    resp = await client.post("/v1/training/filter/self-improve")
    assert resp.status_code == 201, resp.text
    payload = resp.json()
    assert payload["applied"] is False
    assert payload["recalibration"]["version"] == 1
    assert payload["recalibration"]["is_current"] is True
    assert payload["recalibration"]["metrics"]["correction_rate"] == 1.0
    assert payload["recalibration"]["metrics"]["over_refinement_rate"] == 1.0

    names = {p["name"] for p in payload["proposals"]}
    assert "critic_iterations_cap" in names
    assert "input_guard_severity" in names
    assert "grounding_min_evidence" in names
    assert "persona_style_enforcement" in names

    resp = await client.get("/v1/training/filter/recalibration")
    assert resp.status_code == 200
    assert resp.json()["version"] == 1


async def test_filter_self_improve_versions_rollback_and_delete(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await seed_ledger_and_signals(db_session)
    await grant_filter_consent(client)

    resp = await client.post("/v1/training/filter/self-improve")
    assert resp.status_code == 201
    first_version = resp.json()["recalibration"]["version"]

    # More positive signals change the report -> v2.
    db_session.add(
        ResponseLog(
            request_text="Great reply.",
            reply_text="Thanks!",
            mode="casual",
            strategy={},
            provenance_ids=[],
            context_tokens=10,
            was_useful=True,
        )
    )
    await db_session.commit()
    resp = await client.post("/v1/training/filter/self-improve")
    assert resp.status_code == 201
    assert resp.json()["recalibration"]["version"] == 2

    resp = await client.get("/v1/training/filter/recalibration/history")
    assert resp.status_code == 200
    assert len(resp.json()) == 2

    resp = await client.post(
        "/v1/training/filter/recalibration/rollback",
        json={"target_version": first_version, "reason": "revert report"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == first_version
    assert resp.json()["is_current"] is True

    resp = await client.post("/v1/training/filter/recalibration/delete")
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] == 2

    rows = list(
        (await db_session.execute(select(FilterRecalibration))).scalars().all()
    )
    assert len(rows) == 2
    assert all(row.redacted is True for row in rows)
    assert all(row.metrics == {} for row in rows)
    assert all(row.proposals == [] for row in rows)
