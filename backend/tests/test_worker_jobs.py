"""Focused tests for RQ worker-boundary behavior."""

from __future__ import annotations

import asyncio

import pytest


def test_run_research_job_resolves_dead_letter_after_success(monkeypatch) -> None:
    from app.services import runtime
    from app.workers import jobs

    resolved: list[dict] = []

    def fake_run(coroutine):
        coroutine.close()
        return {"status": "completed"}

    def fake_resolve(**kwargs) -> None:
        resolved.append(kwargs)

    monkeypatch.setattr(asyncio, "run", fake_run)
    monkeypatch.setattr(runtime, "resolve_dead_letter_sync", fake_resolve)

    assert jobs.run_research_job("research-123") == {"status": "completed"}
    assert resolved == [{"queue": "research", "job_id": "research-123"}]


def test_run_research_job_records_dead_letter_and_reraises(monkeypatch) -> None:
    from app.services import runtime
    from app.workers import jobs

    failure = RuntimeError("provider unavailable")
    recorded: list[dict] = []

    def fake_run(coroutine):
        coroutine.close()
        raise failure

    def fake_record(**kwargs) -> None:
        recorded.append(kwargs)

    monkeypatch.setattr(asyncio, "run", fake_run)
    monkeypatch.setattr(runtime, "record_dead_letter_sync", fake_record)

    with pytest.raises(RuntimeError, match="provider unavailable"):
        jobs.run_research_job("research-456")

    assert recorded == [
        {
            "queue": "research",
            "job_id": "research-456",
            "payload": {
                "research_job_id": "research-456",
                "entrypoint": "app.workers.jobs.run_research_job",
                "args": ["research-456"],
            },
            "error": "RuntimeError: provider unavailable",
        }
    ]
