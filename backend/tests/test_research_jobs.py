"""Real HTTP and durable-state tests for bounded research jobs."""

from __future__ import annotations

import asyncio

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.research import ResearchService
from app.schemas import ResearchJobCreate
from app.search.providers import MockSearchProvider


async def test_research_job_local_double_persists_evidence_and_is_idempotent(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "search_provider", "mock")
    created = await client.post(
        "/v1/research/jobs",
        json={
            "goal": "Compare local embedding models",
            "allowed_tools": ["web_search"],
            "max_results": 2,
            "checkpoints": ["collect_sources", "preserve_citations"],
        },
    )
    assert created.status_code == 201, created.text
    job = created.json()
    assert job["id"]
    assert job["owner"] == "master"
    assert job["status"] == "queued"
    assert job["budget"]["max_results"] == 2
    assert job["progress"]["phase"] == "queued"

    ran = await client.post(f"/v1/research/jobs/{job['id']}/run")
    assert ran.status_code == 200, ran.text
    result = ran.json()
    assert result["status"] == "completed"
    assert result["progress"]["percent"] == 100
    assert result["final_artifacts"][0]["kind"] == "research_notes"
    assert result["citations"]
    assert result["evidence"]["source"] == "mock"
    assert result["evidence"]["observed"] is True

    replay = await client.post(f"/v1/research/jobs/{job['id']}/run")
    assert replay.status_code == 200, replay.text
    assert replay.json()["status"] == "completed"

    loaded = await client.get(f"/v1/research/jobs/{job['id']}")
    assert loaded.status_code == 200
    assert loaded.json()["citations"] == result["citations"]


async def test_research_job_missing_provider_is_honest_not_connected(
    client: AsyncClient, monkeypatch
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "search_provider", "none")
    created = await client.post(
        "/v1/research/jobs", json={"goal": "Research without a provider"}
    )
    job_id = created.json()["id"]
    ran = await client.post(f"/v1/research/jobs/{job_id}/run")
    assert ran.status_code == 200, ran.text
    result = ran.json()
    assert result["status"] == "failed"
    assert result["last_error"] == "provider_not_connected"
    assert result["evidence"]["error"] == "not_connected"
    assert result["evidence"]["observed"] is False
    assert result["citations"] == []


async def test_research_job_timeout_can_resume_after_worker_restart(
    db_session: AsyncSession,
) -> None:
    class SlowProvider:
        name = "slow-test"

        async def search(self, query: str, *, limit: int = 5):
            await asyncio.sleep(0.05)
            return []

    service = ResearchService(db_session, actor="master")
    job = await service.create_job(
        ResearchJobCreate(goal="Timeout then resume", timeout_seconds=0.01)
    )
    await db_session.commit()

    timed_out = await service.run_job(job.id, provider=SlowProvider())
    assert timed_out["status"] == "paused"
    assert timed_out["last_error"] == "provider_timeout"
    await db_session.commit()

    # A fresh service/session boundary observes the durable checkpoint, as a
    # restarted worker would, and can resume with a working local double.
    from app.db import SessionLocal

    async with SessionLocal() as restarted_session:
        restarted = ResearchService(restarted_session, actor="master")
        resumed = await restarted.resume_job(job.id)
        assert resumed["status"] == "queued"
        await restarted_session.commit()
        completed = await restarted.run_job(job.id, provider=MockSearchProvider())
        assert completed["status"] == "completed"
        assert completed["attempts"] == 2
        assert completed["evidence"]["observed"] is True


async def test_research_job_cancel_is_durable_and_blocks_side_effects(
    client: AsyncClient, monkeypatch
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "search_provider", "mock")
    created = await client.post("/v1/research/jobs", json={"goal": "Cancel this job"})
    assert created.status_code == 201
    job_id = created.json()["id"]
    cancelled = await client.post(f"/v1/research/jobs/{job_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["cancel_requested"] is True

    run = await client.post(f"/v1/research/jobs/{job_id}/run")
    assert run.status_code == 200, run.text
    assert run.json()["status"] == "cancelled"
    assert run.json()["citations"] == []

    resume = await client.post(f"/v1/research/jobs/{job_id}/resume")
    assert resume.status_code == 400
    assert "cannot be resumed" in resume.json()["detail"]
