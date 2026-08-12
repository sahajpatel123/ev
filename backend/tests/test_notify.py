"""PULSE notification delivery: policy, receipts, digest, DLQ escalation."""

from __future__ import annotations

import hashlib
import hmac
from uuid import UUID

import pytest
from sqlalchemy import select

from app.config import settings
from app.models import Alert, Notification
from app.services.runtime import daemon_tick


def _quiet_hours_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin quiet hours off so tests are deterministic at any wall-clock time."""
    monkeypatch.setattr("app.notify.service.quiet_hours_active", lambda now=None: False)
    monkeypatch.setattr("app.notify.policy.quiet_hours_active", lambda now=None: False)


def _quiet_hours_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.notify.service.quiet_hours_active", lambda now=None: True)
    monkeypatch.setattr("app.notify.policy.quiet_hours_active", lambda now=None: True)


async def test_console_backend_delivers_and_records_receipt(
    client, monkeypatch
) -> None:
    _quiet_hours_off(monkeypatch)
    resp = await client.post(
        "/v1/runtime/notify",
        json={"title": "Tea is ready", "body": "Steeped for exactly four minutes."},
    )
    assert resp.status_code == 201, resp.text
    row = resp.json()
    assert row["status"] == "delivered"
    assert row["backend"] == "console"
    assert row["delivered_at"] is not None

    resp = await client.get("/v1/runtime/notifications?status=delivered")
    assert resp.status_code == 200
    assert any(item["id"] == row["id"] for item in resp.json())


async def test_quiet_hours_suppress_non_emergency_but_allow_emergency(
    client, monkeypatch
) -> None:
    _quiet_hours_on(monkeypatch)

    resp = await client.post(
        "/v1/runtime/notify",
        json={"title": "Reminder", "body": "Take a break", "priority": 0.4},
    )
    assert resp.status_code == 201, resp.text
    row = resp.json()
    assert row["status"] == "suppressed"
    assert row["reason"] == "quiet_hours"

    resp = await client.post(
        "/v1/runtime/notify",
        json={"title": "Emergency", "body": "Deadline moved to tonight", "priority": 0.4, "emergency": True},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "delivered"

    resp = await client.post(
        "/v1/runtime/notify",
        json={"title": "Urgent", "body": "Server is down", "priority": 0.95},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "delivered"


async def test_daily_cap_suppresses_with_reason(client, monkeypatch) -> None:
    _quiet_hours_off(monkeypatch)
    monkeypatch.setattr(settings, "daily_alert_budget", 1)

    first = await client.post(
        "/v1/runtime/notify",
        json={"title": "One", "body": "First of the day"},
    )
    assert first.status_code == 201
    assert first.json()["status"] == "delivered"

    second = await client.post(
        "/v1/runtime/notify",
        json={"title": "Two", "body": "Past the cap"},
    )
    assert second.status_code == 201
    assert second.json()["status"] == "suppressed"
    assert second.json()["reason"] == "daily_cap"


async def test_duplicate_fingerprint_is_suppressed(client, monkeypatch) -> None:
    _quiet_hours_off(monkeypatch)
    payload = {"title": "Same", "body": "Same body"}
    first = await client.post("/v1/runtime/notify", json=payload)
    assert first.status_code == 201
    assert first.json()["status"] == "delivered"

    second = await client.post("/v1/runtime/notify", json=payload)
    assert second.status_code == 201
    assert second.json()["status"] == "suppressed"
    assert second.json()["reason"] == "duplicate"


async def test_alert_radar_pending_alert_is_delivered_by_daemon(
    client, db_session, monkeypatch
) -> None:
    _quiet_hours_off(monkeypatch)
    await client.post(
        "/v1/alerts/watchlist",
        json={"kind": "topic", "value": "notify topic", "priority": 0.4, "metadata": {}},
    )
    await client.post(
        "/v1/events",
        json={"source": "test", "event_type": "note", "text": "thinking about the notify topic"},
    )
    scan = await client.get("/v1/alerts/scan?window_days=30")
    assert scan.status_code == 200, scan.text
    assert scan.json()["alerts_created"]

    report = await daemon_tick(db_session)
    await db_session.commit()
    assert report["notifications"]["delivered"] >= 1

    alert = (
        await db_session.execute(
            select(Alert).where(Alert.kind == "watch_match", Alert.status == "delivered")
        )
    ).scalars().first()
    assert alert is not None
    assert "delivery_receipt" in (alert.details or {})
    receipt = (await db_session.execute(
        select(Notification).where(Notification.alert_id == alert.id)
    )).scalars().first()
    assert receipt is not None
    assert receipt.status == "delivered"


async def test_digest_delivers_and_marks_alerts_with_receipt(
    client, db_session, monkeypatch
) -> None:
    _quiet_hours_off(monkeypatch)
    await client.post(
        "/v1/alerts/watchlist",
        json={"kind": "topic", "value": "digest notify topic", "priority": 0.4, "metadata": {}},
    )
    await client.post(
        "/v1/events",
        json={
            "source": "test",
            "event_type": "note",
            "text": "digest notify topic came up again",
        },
    )
    await client.get("/v1/alerts/scan?window_days=30")

    resp = await client.post("/v1/runtime/digest")
    assert resp.status_code == 200, resp.text
    digest = resp.json()
    assert digest["schema_version"] == "ev.runtime.digest.v1"
    assert digest["delivered"] >= 1
    assert all(item["tier"] in ("useful", "background") for item in digest["alerts"])

    pending = (await client.get("/v1/alerts?status=pending")).json()
    assert pending == []
    digest_row = (
        await db_session.execute(
            select(Notification).where(Notification.kind == "digest")
        )
    ).scalars().first()
    assert digest_row is not None
    assert digest_row.status == "delivered"
    assert digest_row.details["digest_id"] == digest["digest_id"]


async def test_digest_suppressed_when_daily_cap_reached(
    client, db_session, monkeypatch
) -> None:
    _quiet_hours_off(monkeypatch)
    monkeypatch.setattr(settings, "daily_alert_budget", 0)
    await client.post(
        "/v1/alerts/watchlist",
        json={"kind": "topic", "value": "cap topic", "priority": 0.4, "metadata": {}},
    )
    await client.post(
        "/v1/events",
        json={"source": "test", "event_type": "note", "text": "cap topic again"},
    )
    await client.get("/v1/alerts/scan?window_days=30")

    resp = await client.post("/v1/runtime/digest")
    assert resp.status_code == 200, resp.text
    assert resp.json()["suppressed"] >= 1
    alerts = (
        await db_session.execute(select(Alert).where(Alert.status == "suppressed"))
    ).scalars().all()
    assert alerts
    assert all((a.details or {}).get("suppression_reason") == "daily_cap" for a in alerts)


async def test_dlq_discard_escalates_once(client, db_session, monkeypatch) -> None:
    _quiet_hours_off(monkeypatch)
    payload = {
        "queue": "ingestion",
        "job_id": "notify-dlq",
        "payload": {"event_id": "n1"},
        "error": "permanent boom",
    }
    for _ in range(3):
        await client.post("/v1/runtime/dead-letters", json=payload)
    discarded = (await client.get("/v1/runtime/dead-letters?status=discarded")).json()
    assert discarded

    first = await daemon_tick(db_session)
    await db_session.commit()
    assert first["dlq_escalations"] == 1
    rows = (
        await db_session.execute(
            select(Notification).where(Notification.kind == "dead_letter")
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "delivered"

    second = await daemon_tick(db_session)
    await db_session.commit()
    assert second["dlq_escalations"] == 0


async def test_action_notification_dispatches_and_preserves_caller_result(
    client, db_session, monkeypatch
) -> None:
    _quiet_hours_off(monkeypatch)
    resp = await client.post(
        "/v1/runtime/actions",
        json={
            "action_type": "notification",
            "title": "Demo reminder",
            "payload": {"text": "Demo in 30m"},
        },
    )
    assert resp.status_code == 201, resp.text
    action = resp.json()
    await client.post(f"/v1/runtime/actions/{action['id']}/approve")
    executed = await client.post(
        f"/v1/runtime/actions/{action['id']}/execute",
        json={"result": {"delivered": True}},
    )
    assert executed.status_code == 200, executed.text
    # Caller-supplied result is preserved for API compatibility; the real
    # receipt lives in the notifications ledger.
    assert executed.json()["result"] == {"delivered": True}
    receipt = (
        await db_session.execute(
            select(Notification).where(
                Notification.action_id == UUID(action["id"]),
                Notification.kind == "action:notification",
            )
        )
    ).scalars().first()
    assert receipt is not None
    assert receipt.status == "delivered"
    assert receipt.backend == "console"


async def test_webhook_backend_signs_and_delivers(
    client, monkeypatch, db_session
) -> None:
    _quiet_hours_off(monkeypatch)
    monkeypatch.setattr(settings, "notify_backend", "webhook")
    monkeypatch.setattr(settings, "notify_webhook_url", "http://hook.test/ev")
    monkeypatch.setattr(settings, "notify_webhook_secret", "s3cret")

    captured: dict = {}

    class FakeResponse:
        status_code = 200
        headers = {"X-EV-Delivery-Id": "delivery-1"}
        text = "ok"

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, content=None, headers=None):
            captured["url"] = url
            captured["content"] = content
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(
        "app.notify.backends.webhook.httpx.AsyncClient",
        lambda *args, **kwargs: FakeClient(),
    )

    resp = await client.post(
        "/v1/runtime/notify",
        json={"title": "Webhook", "body": "Hello phone"},
    )
    assert resp.status_code == 201, resp.text
    row = resp.json()
    assert row["status"] == "delivered"
    assert row["backend"] == "webhook"
    assert row["backend_ref"] == "delivery-1"
    expected = hmac.new(
        b"s3cret", captured["content"], hashlib.sha256
    ).hexdigest()
    assert captured["headers"]["X-EV-Signature"] == f"sha256={expected}"


async def test_macos_backend_fails_honestly_when_unavailable(
    client, monkeypatch
) -> None:
    _quiet_hours_off(monkeypatch)
    monkeypatch.setattr(settings, "notify_backend", "macos")
    monkeypatch.setattr(settings, "notify_macos_allow_osascript", False)

    def _no_helper(self):
        from app.notify.models import NotifierError

        raise NotifierError("no helper on this host")

    monkeypatch.setattr(
        "app.notify.backends.macos.MacOSNotifier._build_helper", _no_helper
    )
    monkeypatch.setattr(
        "app.notify.backends.macos.MacOSNotifier._helper_path", lambda self: None
    )
    monkeypatch.setattr(
        "app.notify.backends.macos.shutil.which", lambda name: None
    )

    resp = await client.post(
        "/v1/runtime/notify",
        json={"title": "Mac", "body": "This should fail honestly"},
    )
    assert resp.status_code == 201, resp.text
    row = resp.json()
    assert row["status"] == "failed"
    assert "macos backend unavailable" in (row["reason"] or "")

    status = (await client.get("/v1/runtime/notify/status")).json()
    assert status["available"] is False


async def test_apns_backend_is_inert_until_token_registered(
    client, monkeypatch
) -> None:
    _quiet_hours_off(monkeypatch)
    monkeypatch.setattr(settings, "notify_backend", "apns")
    resp = await client.post(
        "/v1/runtime/notify",
        json={"title": "APNs", "body": "Should stay inert"},
    )
    assert resp.status_code == 201, resp.text
    row = resp.json()
    assert row["status"] == "failed"
    assert "apns_inert" in (row["reason"] or "")
