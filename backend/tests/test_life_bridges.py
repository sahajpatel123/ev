"""WAVE LIFE: macos_life helper bridge, standing authority, device proxy."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.integrations.life_helper import (
    LifeHelperError,
    LifePermissionDeniedError,
    LifeTimeoutError,
    run_life_helper,
)
from app.integrations.life_policy import evaluate_life_policy
from app.main import app
from app.models import LifeOutboundAction, LiveEvent

MOCK_HELPER = r'''#!/usr/bin/env python3
import json
import os
import sys

command = sys.argv[1]
raw = sys.argv[2:]
args = {}
for index in range(0, len(raw) - 1, 2):
    args[raw[index]] = raw[index + 1]

if os.environ.get("MOCK_LIFE_TIMEOUT") == "1":
    import time
    time.sleep(30)
    sys.exit(0)

if os.environ.get("MOCK_LIFE_INVALID_JSON") == "1":
    print("not json")
    sys.exit(0)

if os.environ.get("MOCK_LIFE_EXIT") == "3":
    print(json.dumps({"ok": False, "error": {
        "code": "permission_denied", "message": "denied"}}))
    sys.exit(3)

if os.environ.get("MOCK_LIFE_EXIT") == "1":
    print(json.dumps({"ok": False, "error": {
        "code": "failed", "message": "boom"}}))
    sys.exit(1)

if os.environ.get("MOCK_LIFE_EXIT") == "4":
    print(json.dumps({"ok": False, "error": {
        "code": "not_available", "message": "not available"}}))
    sys.exit(4)

if os.environ.get("MOCK_LIFE_EXIT") == "5":
    print(json.dumps({"ok": False, "error": {
        "code": "bad_arguments", "message": "bad args"}}))
    sys.exit(5)

if command == "messages.list":
    print(json.dumps({"ok": True, "data": {"messages": [
        {"id": "m1", "handle": "Mom", "text": "Dinner?",
         "date": "2026-08-12T10:00:00Z"}]}}))
    sys.exit(0)

if command == "contacts.resolve":
    print(json.dumps({"ok": True, "data": {"query": args.get("--query"),
        "matches": [
            {"id": "c1", "full_name": "Mom", "phone_numbers": ["+15551234567"],
             "email_addresses": []}]}}))
    sys.exit(0)

if command == "contacts.list":
    print(json.dumps({"ok": True, "data": {"contacts": [
        {"id": "c1", "full_name": "Mom", "phone_numbers": ["+15551234567"],
         "email_addresses": []}]}}))
    sys.exit(0)

if command in ("messages.send", "mail.send"):
    data = {"to": args.get("--to") or args.get("--subject")}
    if os.environ.get("MOCK_LIFE_NO_EVIDENCE") == "1":
        data["dry_run"] = True
    else:
        data["sent"] = True
    print(json.dumps({"ok": True, "data": data}))
    sys.exit(0)

if command == "call.place":
    data = {"destination": args.get("--destination"), "kind": args.get("--kind")}
    if os.environ.get("MOCK_LIFE_NO_EVIDENCE") == "1":
        data["dry_run"] = True
    else:
        data["opened"] = True
    print(json.dumps({"ok": True, "data": data}))
    sys.exit(0)

if command == "apps.activate":
    print(json.dumps({"ok": True, "data": {
        "bundle_identifier": args.get("--bundle-id"),
        "activated": True,
        "launched": True,
    }}))
    sys.exit(0)

if command == "apps.quit":
    print(json.dumps({"ok": True, "data": {
        "bundle_identifier": args.get("--bundle-id"),
        "quit": True,
        "was_running": True,
        "already_closed": False,
    }}))
    sys.exit(0)

if command == "open.url":
    print(json.dumps({"ok": True, "data": {
        "url": args.get("--url"),
        "opened": True,
    }}))
    sys.exit(0)

if command in ("mail.list", "call.check", "apps.frontmost"):
    print(json.dumps({"ok": True, "data": {}}))
    sys.exit(0)

print(json.dumps({"ok": False, "error": {"code": "bad_arguments",
                                          "message": "unknown command"}}))
sys.exit(5)
'''


@pytest.fixture
def mock_life_helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "EVLifeHelper"
    path.write_text(MOCK_HELPER, encoding="utf-8")
    path.chmod(0o755)
    monkeypatch.setattr(settings, "life_helper_path", str(path))
    return path


@pytest.fixture(autouse=True)
def reset_life_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "MOCK_LIFE_EXIT",
        "MOCK_LIFE_TIMEOUT",
        "MOCK_LIFE_INVALID_JSON",
        "MOCK_LIFE_NO_EVIDENCE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(settings, "messaging_provider", "local")
    monkeypatch.setattr(settings, "life_autonomy", "default")
    monkeypatch.setattr(settings, "life_contact_allowlist", "all")
    monkeypatch.setattr(settings, "life_confirm_unknown", True)
    monkeypatch.setattr(settings, "life_helper_timeout_seconds", 2.0)


async def install(client: AsyncClient, adapter: str, **overrides) -> dict:
    defaults = {
        "messaging": ["messaging:read"],
        "contacts": ["contacts:read"],
        "phone": ["phone:act"],
        "mail": ["mail:read"],
        "device_proxy": ["messaging:act", "phone:act"],
    }
    payload = {
        "adapter": adapter,
        "name": f"My {adapter}",
        "scopes": overrides.get("scopes") or defaults[adapter],
        **overrides,
    }
    resp = await client.post("/v1/integrations", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def run_action(
    client: AsyncClient,
    integration_id: str,
    action: str,
    args: dict,
) -> httpx.Response:
    return await client.post(
        f"/v1/integrations/{integration_id}/actions",
        json={"action": action, "args": args},
    )


async def test_macos_life_list_messages_parses_helper_json(
    client: AsyncClient,
    mock_life_helper: Path,
) -> None:
    integration = await install(
        client,
        "messaging",
        scopes=["messaging:read"],
        config={"provider": "macos_life"},
    )
    resp = await run_action(
        client,
        integration["id"],
        "messaging.list_messages",
        {"limit": 10},
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert result["mode"] == "macos_life"
    assert result["messages"][0]["handle"] == "Mom"
    # No OAuth credential was stored: macos_life is helper/TCC-authenticated.


async def test_macos_life_send_returns_real_delivery_evidence(
    client: AsyncClient,
    mock_life_helper: Path,
) -> None:
    integration = await install(
        client,
        "messaging",
        scopes=["messaging:read", "messaging:act"],
        config={"provider": "macos_life", "contact_allowlist": "any"},
    )
    resp = await run_action(
        client,
        integration["id"],
        "messaging.send",
        {"to": "Mom", "text": "I'm late"},
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert result["delivery"]["confirmed"] is True
    assert result["delivery"]["evidence"]["confirmed_by"] == "sent"
    assert result["delivery"]["evidence"]["to"] == "Mom"
    assert result["policy"]["allowed"] is True


async def test_helper_permission_denied_is_loud_never_success(
    client: AsyncClient,
    mock_life_helper: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MOCK_LIFE_EXIT", "3")
    integration = await install(
        client,
        "messaging",
        scopes=["messaging:read", "messaging:act"],
        config={"provider": "macos_life", "contact_allowlist": "any"},
    )
    resp = await run_action(
        client,
        integration["id"],
        "messaging.send",
        {"to": "Mom", "text": "hi"},
    )
    assert resp.status_code == 403, resp.text
    assert "permission denied" in resp.json()["detail"]
    assert "ok" not in resp.json()


async def test_helper_failure_exit_code_maps_to_error(
    client: AsyncClient,
    mock_life_helper: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MOCK_LIFE_EXIT", "1")
    integration = await install(
        client,
        "messaging",
        scopes=["messaging:read"],
        config={"provider": "macos_life"},
    )
    resp = await run_action(client, integration["id"], "messaging.list_messages", {})
    assert resp.status_code == 502, resp.text
    assert "failed" in resp.json()["detail"]


async def test_no_silent_local_fallback_when_macos_life_configured(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "life_helper_path", "")
    integration = await install(
        client,
        "messaging",
        scopes=["messaging:read", "messaging:act"],
        config={"provider": "macos_life", "contact_allowlist": "any"},
    )
    resp = await run_action(
        client,
        integration["id"],
        "messaging.send",
        {"to": "Mom", "text": "hi"},
    )
    assert resp.status_code == 502, resp.text
    assert "EV_LIFE_HELPER_PATH is not set" in resp.json()["detail"]

    # Even without any provider, send never fakes success.
    local = await install(
        client,
        "messaging",
        scopes=["messaging:read", "messaging:act"],
        slug="messaging-local",
    )
    resp = await client.post(
        f"/v1/integrations/{local['id']}/credentials",
        json={
            "access_token": "local-token-123456",
            "refresh_token": "local-refresh-123456",
            "provider_account_id": "owner@example.com",
        },
    )
    assert resp.status_code == 201, resp.text
    resp = await run_action(
        client,
        local["id"],
        "messaging.send",
        {"to": "Mom", "text": "hi"},
    )
    assert resp.status_code == 502, resp.text
    assert "refusing to fake" in resp.json()["detail"]


async def test_send_without_delivery_evidence_fails_loudly(
    client: AsyncClient,
    mock_life_helper: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MOCK_LIFE_NO_EVIDENCE", "1")
    integration = await install(
        client,
        "messaging",
        scopes=["messaging:read", "messaging:act"],
        config={"provider": "macos_life", "contact_allowlist": "any"},
    )
    resp = await run_action(
        client,
        integration["id"],
        "messaging.send",
        {"to": "Mom", "text": "hi"},
    )
    assert resp.status_code == 502, resp.text
    assert "delivery evidence" in resp.json()["detail"]
    assert "dry_run is not delivery" in resp.json()["detail"]


async def test_contacts_and_phone_adapters_use_helper(
    client: AsyncClient,
    mock_life_helper: Path,
) -> None:
    contacts = await install(
        client,
        "contacts",
        config={"provider": "macos_life"},
    )
    resp = await run_action(
        client,
        contacts["id"],
        "contacts.resolve",
        {"query": "Mom"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"]["matches"][0]["full_name"] == "Mom"
    assert resp.json()["result"]["matches"][0]["phone_numbers"] == ["+15551234567"]

    phone = await install(
        client,
        "phone",
        config={"provider": "macos_life", "contact_allowlist": "any"},
    )
    resp = await run_action(client, phone["id"], "phone.call", {"to": "Mom"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"]["delivery"]["evidence"]["confirmed_by"] == "opened"
    assert resp.json()["result"]["delivery"]["evidence"]["destination"] == "Mom"
    resp = await run_action(client, phone["id"], "facetime.call", {"to": "Mom"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"]["delivery"]["evidence"]["kind"] == "facetime"


async def test_mail_unsupported_command_fails_loudly(
    client: AsyncClient,
    mock_life_helper: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MOCK_LIFE_EXIT", "4")
    mail = await install(
        client,
        "mail",
        scopes=["mail:read", "mail:act"],
        config={"provider": "macos_life"},
    )
    resp = await run_action(client, mail["id"], "mail.list", {})
    assert resp.status_code == 502, resp.text
    assert "not_available" in resp.json()["detail"] or "not available" in resp.json()["detail"]


async def test_mail_send_returns_real_evidence(
    client: AsyncClient,
    mock_life_helper: Path,
) -> None:
    mail = await install(
        client,
        "mail",
        scopes=["mail:read", "mail:act"],
        config={"provider": "macos_life", "contact_allowlist": "any"},
    )
    resp = await run_action(
        client,
        mail["id"],
        "mail.send",
        {"to": "mom@example.com", "subject": "Dinner", "body": "On my way"},
    )
    assert resp.status_code == 200, resp.text
    evidence = resp.json()["result"]["delivery"]["evidence"]
    assert evidence["confirmed_by"] == "sent"
    assert evidence["to"] == "mom@example.com"


def test_standing_authority_policy_matrix() -> None:
    scopes = ["messaging:act", "phone:act", "contacts:read"]
    mom = {"id": "c1", "name": "Mom", "phone": "+15551234567", "starred": True}

    # Missing scope fails closed.
    denied = evaluate_life_policy(
        scopes=["messaging:read"],
        action="messaging.send",
        recipient="Mom",
    )
    assert denied.allowed is False
    assert "not granted" in denied.reason

    # allowlist=any pre-authorizes everyone.
    any_ok = evaluate_life_policy(
        scopes=scopes,
        action="messaging.send",
        recipient="+1999",
        allowlist="any",
    )
    assert any_ok.allowed is True
    assert any_ok.confirmation_required is False

    # allowlist=all: known contacts pre-authorized.
    known = evaluate_life_policy(
        scopes=scopes,
        action="messaging.send",
        recipient="Mom",
        contact=mom,
        allowlist="all",
    )
    assert known.allowed is True
    assert known.confirmation_required is False

    # allowlist=all: unknown recipient needs explicit confirmation.
    unknown = evaluate_life_policy(
        scopes=scopes,
        action="messaging.send",
        recipient="+1999",
        allowlist="all",
    )
    assert unknown.allowed is False
    assert unknown.confirmation_required is True
    confirmed = evaluate_life_policy(
        scopes=scopes,
        action="messaging.send",
        recipient="+1999",
        allowlist="all",
        confirm=True,
    )
    assert confirmed.allowed is True

    # allowlist=starred: non-starred known contact still needs confirmation.
    bob = {"id": "c2", "name": "Bob", "phone": "+15559876543", "starred": False}
    starred = evaluate_life_policy(
        scopes=scopes,
        action="phone.call",
        recipient="Bob",
        contact=bob,
        allowlist="starred",
    )
    assert starred.allowed is False
    assert starred.confirmation_required is True

    # EV_LIFE_AUTONOMY=full skips confirmation inside granted scopes.
    full = evaluate_life_policy(
        scopes=scopes,
        action="phone.call",
        recipient="+1999",
        autonomy="full",
    )
    assert full.allowed is True
    assert full.confirmation_required is False


async def test_device_proxy_queue_outbox_and_delivery_evidence(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    proxy = await install(
        client,
        "device_proxy",
        config={"provider": "device_proxy", "contact_allowlist": "any"},
    )
    proxy_id = proxy["id"]

    resp = await run_action(
        client,
        proxy_id,
        "messaging.send",
        {"to": "Mom", "text": "I'm late"},
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert result["mode"] == "device_proxy"
    assert result["queued"] is True
    assert result["delivery"] == {"confirmed": False, "status": "queued"}
    queue_id = UUID(result["queue_id"])

    row = await db_session.get(LifeOutboundAction, queue_id)
    assert row is not None
    assert row.status == "queued"
    assert row.args == {"to": "Mom", "text": "I'm late"}

    # Register an iPhone device and poll the outbox as that device.
    device = (await client.post("/v1/devices", json={"name": "iphone-evie"})).json()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {device['token']}"},
    ) as device_client:
        resp = await device_client.get(f"/v1/integrations/{proxy_id}/life/outbox")
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["id"] == str(queue_id)
        assert items[0]["args"]["text"] == "I'm late"

        # Delivered without evidence: loud failure, still queued.
        resp = await device_client.post(
            f"/v1/integrations/{proxy_id}/life/device-results",
            json={"queue_id": str(queue_id), "status": "delivered"},
        )
        assert resp.status_code == 502, resp.text
        db_session.expire_all()
        row = await db_session.get(LifeOutboundAction, queue_id)
        assert row is not None
        assert row.status == "queued"

        # Delivered WITH provider evidence: confirmed.
        resp = await device_client.post(
            f"/v1/integrations/{proxy_id}/life/device-results",
            json={
                "queue_id": str(queue_id),
                "status": "delivered",
                "evidence": {
                    "message_id": "iphone-m1",
                    "sent_at": "2026-08-12T10:05:00Z",
                },
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "delivered"
        assert resp.json()["delivery"] == {"confirmed": True}

        # Already delivered: cannot re-deliver.
        resp = await device_client.post(
            f"/v1/integrations/{proxy_id}/life/device-results",
            json={"queue_id": str(queue_id), "status": "failed"},
        )
        assert resp.status_code == 400

    db_session.expire_all()
    row = await db_session.get(LifeOutboundAction, queue_id)
    assert row is not None
    assert row.status == "delivered"
    assert (row.evidence or {})["message_id"] == "iphone-m1"
    assert row.delivered_at is not None

    # Outbox is empty now.
    resp = await client.get(f"/v1/integrations/{proxy_id}/life/outbox")
    assert resp.json()["items"] == []


async def test_device_proxy_records_inbound_message_event(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    proxy = await install(
        client,
        "device_proxy",
        config={"provider": "device_proxy"},
    )
    proxy_id = proxy["id"]
    device = (await client.post("/v1/devices", json={"name": "iphone-inbound"})).json()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {device['token']}"},
    ) as device_client:
        resp = await device_client.post(
            f"/v1/integrations/{proxy_id}/life/device-results",
            json={
                "action": "message.received",
                "status": "delivered",
                "message": {
                    "sender": "Mom",
                    "channel": "iMessage",
                    "text": "On my way",
                },
            },
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["accepted"] is True
    rows = (
        await db_session.execute(
            select(LiveEvent).where(
                LiveEvent.channel_id == UUID(proxy["live_channel_id"])
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].event_type == "message.received"
    assert rows[0].payload["text"] == "On my way"


async def test_device_proxy_result_requires_assigned_device(
    client: AsyncClient,
) -> None:
    proxy = await install(
        client,
        "device_proxy",
        config={"provider": "device_proxy", "contact_allowlist": "any"},
    )
    proxy_id = proxy["id"]
    device_a = (await client.post("/v1/devices", json={"name": "iphone-a"})).json()
    device_b = (await client.post("/v1/devices", json={"name": "iphone-b"})).json()
    device_a_id = device_a["device"]["id"]
    device_a_token = device_a["token"]
    device_b_token = device_b["token"]
    resp = await run_action(
        client,
        proxy_id,
        "messaging.send",
        {"to": "Mom", "text": "hi", "device_id": device_a_id},
    )
    assert resp.status_code == 200, resp.text
    queue_id = resp.json()["result"]["queue_id"]

    async def post_result(token: str, queue_id_value: str) -> httpx.Response:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {token}"},
        ) as device_client:
            return await device_client.post(
                f"/v1/integrations/{proxy_id}/life/device-results",
                json={
                    "queue_id": queue_id_value,
                    "status": "delivered",
                    "evidence": {
                        "message_id": "iphone-m2",
                        "sent_at": "2026-08-12T10:06:00Z",
                    },
                },
            )

    # Device B cannot ack device A's action.
    denied = await post_result(device_b_token, queue_id)
    assert denied.status_code == 403, denied.text
    # Device A can.
    ok = await post_result(device_a_token, queue_id)
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "delivered"


async def test_life_policy_endpoint(client: AsyncClient) -> None:
    integration = await install(
        client,
        "messaging",
        scopes=["messaging:read", "messaging:act"],
        config={"provider": "macos_life", "contact_allowlist": "all"},
    )
    resp = await client.get(
        f"/v1/integrations/{integration['id']}/life/policy",
        params={"action": "messaging.send", "recipient": "+1999"},
    )
    assert resp.status_code == 200, resp.text
    policy = resp.json()
    assert policy["allowed"] is False
    assert policy["confirmation_required"] is True


async def test_life_helper_runner_unit(
    mock_life_helper: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await run_life_helper("contacts.resolve", {"query": "Mom"})
    assert result.data["matches"][0]["full_name"] == "Mom"

    monkeypatch.setenv("MOCK_LIFE_EXIT", "3")
    with pytest.raises(LifePermissionDeniedError) as permission_exc:
        await run_life_helper("messages.send", {"to": "Mom", "text": "hi"})
    assert "permission denied" in str(permission_exc.value)

    monkeypatch.setenv("MOCK_LIFE_EXIT", "1")
    with pytest.raises(LifeHelperError) as failure_exc:
        await run_life_helper("messages.list", {})
    assert failure_exc.value.exit_code == 1

    monkeypatch.setenv("MOCK_LIFE_INVALID_JSON", "1")
    with pytest.raises(LifeHelperError) as json_exc:
        await run_life_helper("messages.list", {})
    assert json_exc.value.error_code == "invalid_json"

    monkeypatch.setenv("MOCK_LIFE_TIMEOUT", "1")
    with pytest.raises(LifeTimeoutError):
        await run_life_helper("messages.list", {}, timeout=0.5)


@pytest.mark.skipif(
    not settings.life_helper_path,
    reason="real EVLifeHelper path not configured (manual darwin test)",
)
async def test_real_helper_path_when_configured() -> None:
    """Manual darwin integration: run the real helper when it exists."""
    result = await run_life_helper("apps.frontmost", {})
    assert result.command == "apps.frontmost"
