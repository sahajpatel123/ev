"""PULSE notification delivery: policy, receipts, digest, DLQ escalation."""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.config import settings
from app.main import app
from app.models import Alert, Device, LifeOutboundAction, Notification
from app.notify.registry import ensure_fleet_devices
from app.notify.routing import assign_life_actions
from app.notify.service import dispatch_notification, send_presence_beacon
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


async def test_push_token_register_rotate_and_deregister(
    client, db_session
) -> None:
    resp = await client.post(
        "/v1/devices",
        json={
            "name": "Phone A",
            "device_type": "phone",
            "platform": "apple",
            "capabilities": ["notifications", "attention", "push"],
        },
    )
    assert resp.status_code == 201, resp.text
    device = resp.json()["device"]

    registered = await client.post(
        f"/v1/devices/{device['id']}/push-token",
        json={"token": "ab" * 32, "platform": "apns", "bundle_id": "com.ev.ios"},
    )
    assert registered.status_code == 200, registered.text
    assert registered.json()["registered"] is True
    assert registered.json()["platform"] == "apns"

    status = (await client.get(f"/v1/devices/{device['id']}/status")).json()
    assert status["push_ready"] is True
    assert status["device"]["push_registered"] is True
    assert status["device"]["device_type"] == "phone"

    rotated = await client.post(
        f"/v1/devices/{device['id']}/push-token",
        json={"token": "cd" * 32, "platform": "apns"},
    )
    assert rotated.status_code == 200
    row = await db_session.get(Device, UUID(device["id"]))
    assert row is not None
    assert row.push_token == "cd" * 32

    removed = await client.delete(f"/v1/devices/{device['id']}/push-token")
    assert removed.status_code == 200
    assert removed.json()["registered"] is False
    await db_session.refresh(row)
    assert row.push_token is None


async def test_fleet_registry_seeds_mac_and_two_phones_idempotently(
    db_session,
) -> None:
    first = await ensure_fleet_devices(db_session)
    await db_session.commit()
    assert set(first["created"]) == {"Mac", "Phone A", "Phone B"}

    second = await ensure_fleet_devices(db_session)
    await db_session.commit()
    assert second["created"] == []

    rows = (
        await db_session.execute(
            select(Device).where(Device.name.in_(["Mac", "Phone A", "Phone B"]))
        )
    ).scalars().all()
    types = {row.name: row.device_type for row in rows}
    assert types == {"Mac": "mac", "Phone A": "phone", "Phone B": "phone"}


async def test_apns_routing_fails_honestly_without_credentials(
    db_session, monkeypatch
) -> None:
    _quiet_hours_off(monkeypatch)
    phone = Device(
        name="Phone A",
        device_type="phone",
        platform="apple",
        capabilities=["notifications", "attention", "push"],
        push_token="ab" * 32,
        push_token_updated_at=datetime.now(),
    )
    db_session.add(phone)
    await db_session.flush()

    row = await dispatch_notification(
        db_session,
        title="APNs route",
        body="Should route to the phone and fail honestly",
        device_id=phone.id,
        emergency=True,
        bypass_policy=True,
    )
    await db_session.commit()
    assert row.device_id == phone.id
    assert row.backend == "apns"
    assert row.status == "failed"
    assert "apns_inert" in (row.reason or "")
    assert (row.details or {}).get("routing", {}).get("device_type") == "phone"


async def test_acknowledged_attention_never_re_notifies(
    db_session, monkeypatch
) -> None:
    _quiet_hours_off(monkeypatch)
    row = await dispatch_notification(
        db_session,
        title="Acked",
        body="Already acknowledged",
        attention_kind="acknowledged",
        fingerprint="ack:test:1",
    )
    await db_session.commit()
    assert row.status == "suppressed"
    assert row.reason == "already_acknowledged"


async def test_presence_beacon_sends_once_per_day(
    db_session, monkeypatch
) -> None:
    _quiet_hours_off(monkeypatch)
    first = await send_presence_beacon(db_session)
    await db_session.commit()
    assert first is not None
    assert first.kind == "presence"
    assert first.status == "delivered"
    assert first.attention_kind == "evie_initiated"

    second = await send_presence_beacon(db_session)
    await db_session.commit()
    assert second is None


async def test_life_action_routing_and_lifecycle_end_to_end(
    client, db_session, monkeypatch
) -> None:
    """queued → dispatched → acknowledged → executed, all evidence-backed."""
    _quiet_hours_off(monkeypatch)
    mac_resp = await client.post(
        "/v1/devices",
        json={
            "name": "Mac",
            "device_type": "mac",
            "capabilities": ["attention", "messaging", "call"],
        },
    )
    assert mac_resp.status_code == 201, mac_resp.text
    mac = mac_resp.json()["device"]
    phone_resp = await client.post(
        "/v1/devices",
        json={
            "name": "Phone A",
            "device_type": "phone",
            "capabilities": ["attention", "messaging", "call", "push"],
        },
    )
    assert phone_resp.status_code == 201, phone_resp.text
    phone_payload = phone_resp.json()
    phone = phone_payload["device"]
    # Phone A heartbeats last, so it is the best reachable messaging device.
    await client.post(
        "/v1/runtime/heartbeat",
        json={"device_id": mac["id"], "status": "ok"},
    )
    await client.post(
        "/v1/runtime/heartbeat",
        json={"device_id": phone["id"], "status": "ok"},
    )

    proxy = (
        await client.post(
            "/v1/integrations",
            json={
                "adapter": "device_proxy",
                "name": "Phone proxy",
                "scopes": ["messaging:act", "phone:act"],
                "config": {"provider": "device_proxy", "contact_allowlist": "any"},
            },
        )
    ).json()
    queued = (
        await client.post(
            f"/v1/integrations/{proxy['id']}/actions",
            json={"action": "messaging.send", "args": {"to": "Mom", "text": "On my way"}},
        )
    ).json()["result"]
    queue_id = UUID(queued["queue_id"])

    routed = await assign_life_actions(db_session)
    await db_session.commit()
    assert routed["assigned"] == 1
    row = await db_session.get(LifeOutboundAction, queue_id)
    assert row is not None
    assert row.device_id == UUID(phone["id"])
    assert row.lifecycle == "dispatched"

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {phone_payload['token']}"},
    ) as phone_client:
        claimed = await phone_client.post(f"/v1/runtime/life-jobs/{queue_id}/claim")
        assert claimed.status_code == 200, claimed.text
        assert claimed.json()["lifecycle"] == "acknowledged"

        delivered = await phone_client.post(
            f"/v1/integrations/{proxy['id']}/life/device-results",
            json={
                "queue_id": str(queue_id),
                "status": "delivered",
                "evidence": {
                    "message_id": "sms-42",
                    "sent_at": "2026-08-13T10:00:00Z",
                },
            },
        )
        assert delivered.status_code == 200, delivered.text

    db_session.expire_all()
    report = await daemon_tick(db_session)
    await db_session.commit()
    assert report["life_reconciled"]["executed"] == 1
    await db_session.refresh(row)
    assert row.lifecycle == "executed"


async def test_life_routing_respects_capabilities(client, db_session) -> None:
    """A messaging job must never route to a device without messaging."""
    phone = (
        await client.post(
            "/v1/devices",
            json={
                "name": "Phone A",
                "device_type": "phone",
                "capabilities": ["notifications", "attention"],
            },
        )
    ).json()["device"]
    mac = (
        await client.post(
            "/v1/devices",
            json={
                "name": "Mac",
                "device_type": "mac",
                "capabilities": ["messaging"],
            },
        )
    ).json()["device"]
    await client.post(
        "/v1/runtime/heartbeat",
        json={"device_id": phone["id"], "status": "ok"},
    )
    await client.post(
        "/v1/runtime/heartbeat",
        json={"device_id": mac["id"], "status": "ok"},
    )

    proxy = (
        await client.post(
            "/v1/integrations",
            json={
                "adapter": "device_proxy",
                "name": "Proxy",
                "scopes": ["messaging:act"],
                "config": {"provider": "device_proxy", "contact_allowlist": "any"},
            },
        )
    ).json()
    queued = (
        await client.post(
            f"/v1/integrations/{proxy['id']}/actions",
            json={"action": "messaging.send", "args": {"to": "Mom", "text": "hi"}},
        )
    ).json()["result"]
    queue_id = UUID(queued["queue_id"])

    routed = await assign_life_actions(db_session)
    await db_session.commit()
    assert routed["assigned"] == 1
    row = await db_session.get(LifeOutboundAction, queue_id)
    assert row is not None
    assert row.device_id == UUID(mac["id"])


async def test_life_routing_prefers_reachable_device(
    client, db_session
) -> None:
    """Online beats unknown when both can execute the action."""
    unknown = (
        await client.post(
            "/v1/devices",
            json={
                "name": "Phone A",
                "device_type": "phone",
                "capabilities": ["messaging", "call"],
            },
        )
    ).json()["device"]
    online = (
        await client.post(
            "/v1/devices",
            json={
                "name": "Phone B",
                "device_type": "phone",
                "capabilities": ["messaging", "call"],
            },
        )
    ).json()["device"]
    await client.post(
        "/v1/runtime/heartbeat",
        json={"device_id": online["id"], "status": "ok"},
    )

    proxy = (
        await client.post(
            "/v1/integrations",
            json={
                "adapter": "device_proxy",
                "name": "Proxy",
                "scopes": ["phone:act"],
                "config": {"provider": "device_proxy", "contact_allowlist": "any"},
            },
        )
    ).json()
    queued = (
        await client.post(
            f"/v1/integrations/{proxy['id']}/actions",
            json={"action": "phone.call", "args": {"to": "Mom"}},
        )
    ).json()["result"]
    queue_id = UUID(queued["queue_id"])

    routed = await assign_life_actions(db_session)
    await db_session.commit()
    assert routed["assigned"] == 1
    row = await db_session.get(LifeOutboundAction, queue_id)
    assert row is not None
    assert row.device_id == UUID(online["id"])
    assert unknown["id"] != online["id"]


async def test_notification_receipt_ack_endpoint(
    client, db_session, monkeypatch
) -> None:
    _quiet_hours_off(monkeypatch)
    resp = await client.post(
        "/v1/runtime/notify",
        json={"title": "Ack me", "body": "EV.app should ack this"},
    )
    assert resp.status_code == 201, resp.text
    notification_id = resp.json()["id"]

    device = (
        await client.post(
            "/v1/devices",
            json={"name": "Phone A", "device_type": "phone"},
        )
    ).json()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {device['token']}"},
    ) as device_client:
        ack = await device_client.post(f"/v1/notify/{notification_id}/receipt")
        assert ack.status_code == 200, ack.text
        assert ack.json()["acknowledged"] is True

    row = await db_session.get(Notification, UUID(notification_id))
    assert row is not None
    assert row.attention_kind == "acknowledged"
    assert "acknowledged_at" in (row.details or {})
