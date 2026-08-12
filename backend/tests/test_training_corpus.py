"""Tests for consent-gated training corpus harvesting (Training track 7.4)."""

from __future__ import annotations

import json
import os

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FilterLedger, ResponseLog
from app.training import corpus as corpus_service
from app.utils.text import utcnow


async def post_event(client: AsyncClient, text: str, *, privacy_level: str = "normal") -> dict:
    resp = await client.post(
        "/v1/events",
        json={
            "source": "test",
            "event_type": "note",
            "text": text,
            "privacy_level": privacy_level,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def grant_corpus_consent(client: AsyncClient) -> None:
    resp = await client.post("/v1/training/consent", json={"track": "training_corpus"})
    assert resp.status_code == 201, resp.text


async def seed_rated_response(db_session: AsyncSession) -> None:
    db_session.add(
        ResponseLog(
            request_text="What should I build next?",
            reply_text="Focus on the memory browser.",
            mode="casual",
            strategy={},
            provenance_ids=[],
            context_tokens=20,
            was_useful=True,
        )
    )
    await db_session.commit()


async def seed_filter_ledger(db_session: AsyncSession) -> None:
    db_session.add(
        FilterLedger(
            request_id="test-req-1",
            stage="output",
            action="soften",
            name="grounding",
            detail={"flag": "unsupported claim"},
            draft="That happened for sure.",
            final_text="That may have happened.",
            iterations=1,
        )
    )
    await db_session.commit()


async def test_corpus_build_requires_consent(client: AsyncClient) -> None:
    await post_event(client, "I prefer concise answers.")
    resp = await client.post("/v1/training/corpus/build")
    assert resp.status_code == 403
    assert "training_corpus" in resp.json()["detail"]


async def test_corpus_build_harvests_and_excludes(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await post_event(client, "I prefer concise answers.")
    await post_event(client, "Secret plan never leaves the machine.", privacy_level="never_send_to_model")
    await post_event(client, "Health data is sensitive.", privacy_level="sensitive")
    await post_event(
        client,
        "My API key is sk-1234567890abcdefghijklmnop and I keep it safe.",
    )
    await seed_rated_response(db_session)
    await seed_filter_ledger(db_session)
    await grant_corpus_consent(client)

    resp = await client.post("/v1/training/corpus/build")
    assert resp.status_code == 201, resp.text
    payload = resp.json()
    # The API-key event is escalated to never_send_to_model at ingestion by the
    # PII classifier, so only the plain event is harvestable.
    assert payload["excluded_never_send_to_model"] == 3
    assert payload["entry_count"] == 4  # 2 response + 1 filter + 1 normal event
    assert payload["snapshot"]["version"] == 1
    assert payload["snapshot"]["is_current"] is True
    assert payload["snapshot"]["content_hash"]
    assert payload["snapshot"]["source_counts"]["response_log"] == 2
    assert payload["snapshot"]["source_counts"]["filter_ledger"] == 1
    assert payload["snapshot"]["source_counts"]["events"] == 1

    resp = await client.get("/v1/training/corpus/1/export")
    assert resp.status_code == 200, resp.text
    entries = resp.json()["entries"]
    texts = " ".join(e["text"] for e in entries)
    assert "Secret plan never leaves the machine" not in texts
    assert "Health data is sensitive" not in texts
    assert "sk-1234567890abcdefghijklmnop" not in texts
    assert "API key" not in texts
    assert "Focus on the memory browser." in texts
    assert "That may have happened." in texts


async def test_corpus_reproducible_and_versioned(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await post_event(client, "I prefer concise answers.")
    await seed_rated_response(db_session)
    await grant_corpus_consent(client)

    resp = await client.post("/v1/training/corpus/build")
    assert resp.status_code == 201
    first_hash = resp.json()["snapshot"]["content_hash"]

    # Same sources -> identical deterministic hash.
    resp = await client.post("/v1/training/corpus/build")
    assert resp.status_code == 201
    assert resp.json()["snapshot"]["content_hash"] == first_hash
    assert resp.json()["snapshot"]["version"] == 2

    # New evidence -> v3 with a different hash.
    await post_event(client, "Now I want longer answers.")
    resp = await client.post("/v1/training/corpus/build")
    assert resp.status_code == 201
    v3 = resp.json()["snapshot"]
    assert v3["version"] == 3
    assert v3["content_hash"] != first_hash

    resp = await client.post(
        "/v1/training/corpus/rollback",
        json={"target_version": 1, "reason": "revert to minimal corpus"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == 1
    assert resp.json()["is_current"] is True
    assert resp.json()["content_hash"] == first_hash


async def test_corpus_delete_and_retention_redact(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await post_event(client, "I prefer concise answers.")
    await grant_corpus_consent(client)
    resp = await client.post("/v1/training/corpus/build")
    assert resp.status_code == 201

    resp = await client.post("/v1/training/corpus/delete")
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] == 1
    assert resp.json()["redacted"] is True

    resp = await client.get("/v1/training/corpus/1/export")
    assert resp.status_code == 200
    export = resp.json()
    assert export["snapshot"]["redacted"] is True
    assert export["snapshot"]["content_hash"] is None
    assert export["entries"] == []

    # A redacted snapshot cannot be exported as a training dataset.
    resp = await client.get("/v1/training/corpus/1/dataset")
    assert resp.status_code == 422
    assert "redacted" in resp.json()["detail"]

    # Retention sweep (EU default: 0 days) redacts due snapshots.
    os.environ["EV_REGION"] = "eu"
    try:
        count = await corpus_service.delete_due_snapshots(db_session, now=utcnow())
    finally:
        os.environ.pop("EV_REGION", None)
    assert count == 0  # already redacted

    await grant_corpus_consent(client)
    resp = await client.post("/v1/training/corpus/build")
    assert resp.status_code == 201
    os.environ["EV_REGION"] = "eu"
    try:
        count = await corpus_service.delete_due_snapshots(db_session, now=utcnow())
    finally:
        os.environ.pop("EV_REGION", None)
    assert count == 1


def _parse_ndjson(payload: str) -> list[dict]:
    return [json.loads(line) for line in payload.splitlines() if line.strip()]


async def test_corpus_dataset_jsonl_export(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Snapshot export produces canonical input/output/signals NDJSON."""

    await post_event(client, "I prefer concise answers.")
    await post_event(
        client,
        "Secret plan never leaves the machine.",
        privacy_level="never_send_to_model",
    )
    await seed_rated_response(db_session)
    await seed_filter_ledger(db_session)
    await grant_corpus_consent(client)

    resp = await client.post("/v1/training/corpus/build")
    assert resp.status_code == 201, resp.text
    version = resp.json()["snapshot"]["version"]

    resp = await client.get(f"/v1/training/corpus/{version}/dataset")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    records = _parse_ndjson(resp.text)
    assert records
    for record in records:
        assert "input" in record
        assert "output" in record
        assert "signals" in record
        assert record["hash"]

    texts = " ".join(f"{r['input']} {r['output']}" for r in records)
    assert "Secret plan never leaves the machine" not in texts
    assert "sk-1234567890abcdefghijklmnop" not in texts

    response_pair = next(r for r in records if r["kind"] == "response")
    assert response_pair["input"] == "What should I build next?"
    assert response_pair["output"] == "Focus on the memory browser."
    assert response_pair["signals"]["was_useful"] is True

    filter_pair = next(r for r in records if r["kind"] == "filter")
    assert filter_pair["input"] == "That happened for sure."
    assert filter_pair["output"] == "That may have happened."

    event_record = next(r for r in records if r["kind"] == "event")
    assert event_record["input"] == "I prefer concise answers."
    assert event_record["output"] == ""
