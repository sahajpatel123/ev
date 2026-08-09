"""Ingestion-time PII auto-classification and privacy escalation."""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.live import query_live_events
from app.security.pii import classify_pii, escalate_privacy


def test_pii_classifier_unit() -> None:
    assert classify_pii("call me at +1 (415) 555-0132") == ["phone"]
    assert classify_pii("reach ada@example.com") == ["email"]
    assert classify_pii("ssn 123-45-6789") == ["ssn"]
    assert classify_pii("key sk-abcdefghijklmnopqrstuvwxyz123456") == ["api_key"]
    assert classify_pii("no identifiers here") == []
    # ISO dates must not be misread as phone numbers.
    assert classify_pii("on 2026-08-10 at 11:00") == []
    # UUIDs and ISO timestamps (common in payload metadata) must not be PII.
    assert classify_pii("uuid 12345678-1234-1234-1234-123456789abc") == []
    assert classify_pii("at 2026-08-10T11:00:00Z") == []
    # Hex digests/identifiers with embedded digit runs must not be PII.
    assert classify_pii("sha b0423673071359e7ff62ec7f2766b1bfe") == []
    assert classify_pii("card 4242 4242 4242 4242") == ["card_number"]


def test_escalate_privacy_unit() -> None:
    assert escalate_privacy("normal", ["phone"]) == "sensitive"
    assert escalate_privacy("private", ["email"]) == "sensitive"
    assert escalate_privacy("normal", ["card_number"]) == "never_send_to_model"
    assert escalate_privacy("sensitive", ["api_key"]) == "never_send_to_model"
    assert escalate_privacy("never_send_to_model", ["email"]) == "never_send_to_model"
    assert escalate_privacy("normal", []) == "normal"


async def test_event_ingestion_auto_escalates_pii(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/events",
        json={
            "source": "test",
            "event_type": "note",
            "text": "My contact email is ada@example.com.",
        },
    )
    assert resp.status_code == 201, resp.text
    event = resp.json()["event"]
    assert event["privacy_level"] == "sensitive"
    assert "email" in event["metadata"]["pii_categories"]

    resp = await client.post(
        "/v1/events",
        json={
            "source": "test",
            "event_type": "note",
            "text": "deploy token = a1b2c3d4e5f6g7h8",
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["event"]["privacy_level"] == "never_send_to_model"


async def test_live_event_ingestion_auto_escalates_pii(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    resp = await client.post(
        "/v1/live/events",
        json={
            "channel": "screen-activity",
            "kind": "screen",
            "events": [
                {
                    "event_type": "focus_change",
                    "payload": {"app": "Mail", "text": "forward to ada@example.com"},
                }
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    stored = resp.json()[0]
    assert stored["privacy_level"] == "sensitive"

    # The user slice keeps it; the model slice excludes it by default.
    user_rows = await query_live_events(db_session, access="user")
    model_rows = await query_live_events(db_session, access="model")
    assert any(str(row.id) == stored["id"] for row in user_rows)
    assert all(str(row.id) != stored["id"] for row in model_rows)


async def test_normal_content_untouched(client: AsyncClient) -> None:
    resp = await client.post(
        "/v1/events",
        json={"source": "test", "event_type": "note", "text": "I decided to use SQLite."},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["event"]["privacy_level"] == "normal"
    assert "pii_categories" not in resp.json()["event"]["metadata"]
