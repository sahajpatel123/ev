"""Follow-up Order 6: enrichment is async, batched, deduped, triaged, capped."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts import ChatResult
from app.memory.llm_extractor import (
    measure_enrichment_economics,
    should_enrich,
)
from app.models import Event, Memory
from app.services.llm_extraction import run_llm_extraction_batch, run_llm_extraction_for_event


class FakeDeepSeekProvider:
    name = "deepseek"

    CANDIDATE = {
        "memory_type": "preference",
        "text": "Prefers tea over coffee.",
        "importance": 0.8,
        "confidence": 0.85,
        "source_type": "explicit",
        "entities": [{"name": "tea", "entity_type": "topic"}],
        "payload": {"subject": "tea", "value": "prefer", "over": "coffee"},
    }

    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    async def chat(self, messages, *, model=None, temperature=0.7) -> ChatResult:
        self.calls += 1
        if self.fail:
            raise httpx.ConnectError("network blocked", request=None)
        user_text = next((m.content for m in messages if m.role == "user"), "")
        all_text = " ".join(m.content for m in messages)
        if "Each input line" in all_text:
            indexes = [int(match) for match in re.findall(r"^\[(\d+)\]", user_text, re.M)]
            payload = {
                "results": [
                    {"index": index, "candidates": [self.CANDIDATE]}
                    for index in indexes
                ]
            }
        else:
            payload = {"candidates": [self.CANDIDATE]}
        return ChatResult(
            text=json.dumps(payload),
            usage={"prompt_tokens": 120, "completion_tokens": 40},
            model="deepseek-enrich",
        )

    async def list_models(self):
        return ["deepseek-enrich"]


async def _post(client: AsyncClient, text: str) -> dict:
    resp = await client.post(
        "/v1/events",
        json={"source": "test", "event_type": "note", "text": text},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["event"]


async def _rule_memory_exists(db_session: AsyncSession, text: str) -> bool:
    memories = (await db_session.execute(select(Memory))).scalars().all()
    return any(text in m.text for m in memories)


async def test_ingestion_never_touches_network(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EV_LLM_EXTRACTION_ENABLED", "true")
    from app.gateway import providers as gateway_providers

    monkeypatch.setattr(
        gateway_providers,
        "get_chat_provider",
        lambda: FakeDeepSeekProvider(fail=True),
    )
    event = await _post(client, "I prefer tea over coffee.")
    # The rule path produced memory synchronously even though the enrichment
    # provider would fail on any call.
    assert event["id"]
    assert await _rule_memory_exists(db_session, "Preference: tea")


async def test_enrichment_failure_leaves_rule_memory(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EV_LLM_EXTRACTION_ENABLED", "true")
    from app.gateway import providers as gateway_providers

    provider = FakeDeepSeekProvider(fail=True)
    monkeypatch.setattr(gateway_providers, "get_chat_provider", lambda: provider)
    event = Event(
        source="test",
        event_type="note",
        content={"text": "I prefer tea over coffee."},
        occurred_at=datetime(2026, 8, 1, tzinfo=UTC),
        sha256="0" * 64,
    )
    db_session.add(event)
    await db_session.flush()
    report = await run_llm_extraction_for_event(db_session, event.id, force=True)
    assert report["status"] == "error"
    assert "network blocked" in report["error"]
    assert provider.calls == 1
    assert not await _rule_memory_exists(db_session, "Prefers tea")


async def test_triage_skips_clear_captures(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EV_LLM_EXTRACTION_ENABLED", "true")
    from app.gateway import providers as gateway_providers

    provider = FakeDeepSeekProvider()
    monkeypatch.setattr(gateway_providers, "get_chat_provider", lambda: provider)
    clear = Event(
        source="test",
        event_type="note",
        content={"text": "I prefer tea over coffee."},
        occurred_at=datetime(2026, 8, 1, tzinfo=UTC),
        sha256="0" * 64,
    )
    db_session.add(clear)
    await db_session.flush()
    report = await run_llm_extraction_for_event(db_session, clear.id)
    assert report["status"] == "skipped_triage"
    assert provider.calls == 0

    complex_event = Event(
        source="test",
        event_type="note",
        content={
            "text": (
                "I have been thinking about whether to move to Berlin or Amsterdam "
                "for the new role, and I decided to move to Berlin in March."
            )
        },
        occurred_at=datetime(2026, 8, 1, tzinfo=UTC),
        sha256="1" * 64,
    )
    db_session.add(complex_event)
    await db_session.flush()
    report = await run_llm_extraction_for_event(db_session, complex_event.id)
    assert report["status"] == "ok"
    assert provider.calls == 1


async def test_duplicate_text_is_never_reextracted(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EV_LLM_EXTRACTION_ENABLED", "true")
    from app.gateway import providers as gateway_providers

    provider = FakeDeepSeekProvider()
    monkeypatch.setattr(gateway_providers, "get_chat_provider", lambda: provider)
    first = Event(
        source="test",
        event_type="note",
        content={"text": "I prefer tea over coffee."},
        occurred_at=datetime(2026, 8, 1, tzinfo=UTC),
        sha256="0" * 64,
    )
    second = Event(
        source="test",
        event_type="note",
        content={"text": "I prefer tea over coffee."},
        occurred_at=datetime(2026, 8, 1, 0, 1, tzinfo=UTC),
        sha256="1" * 64,
    )
    db_session.add_all([first, second])
    await db_session.flush()
    assert (await run_llm_extraction_for_event(db_session, first.id, force=True))["status"] == "ok"
    assert (await run_llm_extraction_for_event(db_session, second.id, force=True))["status"] == "duplicate"
    assert provider.calls == 1


async def test_enrichment_usage_endpoint_reports_meter_and_pause(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EV_LLM_EXTRACTION_ENABLED", "true")
    from app.gateway import providers as gateway_providers

    provider = FakeDeepSeekProvider()
    monkeypatch.setattr(gateway_providers, "get_chat_provider", lambda: provider)
    event = await _post(client, "I prefer tea over coffee.")
    report = await run_llm_extraction_for_event(
        db_session,
        UUID(event["id"]),
        force=True,
    )
    assert report["status"] == "ok"
    await db_session.commit()

    resp = await client.get("/v1/enrichment/usage")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is True
    assert body["usage"]["day_calls"] == 1
    assert body["paused"] is False

    monkeypatch.setenv("EV_LLM_EXTRACTION_DAILY_CALL_CAP", "0")
    resp = await client.get("/v1/enrichment/usage")
    assert resp.status_code == 200
    assert resp.json()["paused"] is True


async def test_budget_cap_pauses_enrichment(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EV_LLM_EXTRACTION_ENABLED", "true")
    monkeypatch.setenv("EV_LLM_EXTRACTION_DAILY_CALL_CAP", "0")
    from app.gateway import providers as gateway_providers

    provider = FakeDeepSeekProvider()
    monkeypatch.setattr(gateway_providers, "get_chat_provider", lambda: provider)
    event = Event(
        source="test",
        event_type="note",
        content={"text": "I prefer tea over coffee."},
        occurred_at=datetime(2026, 8, 1, tzinfo=UTC),
        sha256="0" * 64,
    )
    db_session.add(event)
    await db_session.flush()
    report = await run_llm_extraction_for_event(db_session, event.id, force=True)
    assert report["status"] == "budget_paused"
    assert provider.calls == 0


async def test_batch_enrichment_is_batched_and_deduped(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EV_LLM_EXTRACTION_ENABLED", "true")
    monkeypatch.setenv("EV_LLM_EXTRACTION_BATCH_SIZE", "2")
    from app.gateway import providers as gateway_providers

    provider = FakeDeepSeekProvider()
    monkeypatch.setattr(gateway_providers, "get_chat_provider", lambda: provider)
    texts = [
        "I prefer tea over coffee.",
        "I prefer tea over coffee.",
        "I decided to move to Berlin in March.",
        "I'm planning to learn guitar.",
    ]
    events = [
        Event(
            source="test",
            event_type="note",
            content={"text": text},
            occurred_at=datetime(2026, 8, 1, tzinfo=UTC),
            sha256=str(index).zfill(64),
        )
        for index, text in enumerate(texts)
    ]
    db_session.add_all(events)
    await db_session.flush()
    report = await run_llm_extraction_batch(db_session, force=True, limit=10)
    assert report["api_calls"] == 2  # 3 unique texts, batch size 2
    assert report["processed"] == 3
    assert report["skipped"]["duplicate"] == 1


def test_economics_measurement_reports_calls_and_cost() -> None:
    import json as jsonlib
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "eval" / "extraction" / "seed_captures.json"
    captures = jsonlib.loads(path.read_text(encoding="utf-8"))["captures"]
    report = measure_enrichment_economics(captures, batch_size=8)
    assert report["captures"] == len(captures)
    assert report["calls_per_100_captures"] < 100.0
    assert report["api_calls"] > 0
    assert report["estimated_monthly_cost_usd"] >= 0.0
    print(
        "\nENRICHMENT ECONOMICS: "
        f"calls/100={report['calls_per_100_captures']} "
        f"enriched={report['enriched']} triage_skip={report['triage_skip']} "
        f"est_monthly_cost=${report['estimated_monthly_cost_usd']}"
    )


def test_should_enrich_is_deterministic() -> None:
    clear = Event(
        source="test",
        event_type="note",
        content={"text": "I prefer tea over coffee."},
        occurred_at=datetime(2026, 8, 1, tzinfo=UTC),
        sha256="0" * 64,
    )
    assert should_enrich(clear) is False
    long_text = Event(
        source="test",
        event_type="note",
        content={"text": "x" * 120},
        occurred_at=datetime(2026, 8, 1, tzinfo=UTC),
        sha256="1" * 64,
    )
    assert should_enrich(long_text) is True
