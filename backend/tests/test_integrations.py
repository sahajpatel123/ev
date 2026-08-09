"""Integrations & ecosystem: adapters, vault, scopes, webhooks, plugins."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations import vault
from app.integrations.webhooks import (
    SignatureError,
    SlidingWindowRateLimiter,
    verify_webhook_signature,
)
from app.main import app
from app.models import (
    AccessLog,
    Integration,
    IntegrationCredential,
    LiveChannel,
    LiveEvent,
    Plugin,
)
from app.utils.text import sha256_hex

DEFAULT_SCOPES = {
    "calendar": ["calendar:read"],
    "github": ["github:read"],
    "health": ["health:read"],
    "smart_home": ["home:read"],
    "messaging": ["messaging:read"],
}


async def install(client: AsyncClient, adapter: str = "calendar", **overrides) -> dict:
    payload = {
        "adapter": adapter,
        "name": f"My {adapter}",
        "scopes": DEFAULT_SCOPES[adapter],
        **overrides,
    }
    resp = await client.post("/v1/integrations", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def store_oauth(
    client: AsyncClient,
    integration_id: str,
    *,
    token: str = "super-secret-token-123456",
) -> dict:
    resp = await client.post(
        f"/v1/integrations/{integration_id}/credentials",
        json={
            "access_token": token,
            "refresh_token": "refresh-token-123456",
            "provider_account_id": "owner@example.com",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def signed_headers(payload: dict, secret: str, *, timestamp: int | None = None) -> dict[str, str]:
    ts = str(int(timestamp if timestamp is not None else time.time()))
    body = json.dumps(payload).encode("utf-8")
    digest = hmac.new(
        secret.encode("utf-8"),
        f"{ts}.".encode("ascii") + body,
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-EV-Signature": f"sha256={digest}",
        "X-EV-Timestamp": ts,
    }


async def webhook(
    client: AsyncClient,
    integration_id: str,
    payload: dict,
    secret: str,
    *,
    timestamp: int | None = None,
) -> dict:
    resp = await client.post(
        f"/v1/integrations/webhook/{integration_id}",
        content=json.dumps(payload),
        headers=signed_headers(payload, secret, timestamp=timestamp),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_catalog_and_install_validation(client: AsyncClient) -> None:
    resp = await client.get("/v1/integrations/catalog")
    assert resp.status_code == 200
    catalog = {item["adapter"]: item for item in resp.json()}
    assert set(catalog) == {"calendar", "health", "github", "smart_home", "messaging"}
    assert catalog["github"]["capabilities"] == ["github:read", "github:act"]
    assert catalog["health"]["min_privacy"] == "sensitive"
    assert catalog["calendar"]["actions"][0]["name"] == "calendar.list_upcoming"

    integration = await install(client, "calendar", scopes=["calendar:read", "calendar:act"])
    assert integration["status"] == "active"
    assert integration["live_channel_id"]
    assert integration["credential_configured"] is False

    resp = await client.post(
        "/v1/integrations",
        json={"adapter": "calendar", "name": "Bad", "scopes": ["calendar:admin"]},
    )
    assert resp.status_code == 403

    resp = await client.post(
        "/v1/integrations",
        json={"adapter": "not-real", "name": "Bad", "scopes": ["calendar:read"]},
    )
    assert resp.status_code == 400

    resp = await client.post(
        "/v1/integrations",
        json={"adapter": "calendar", "name": "Dup", "scopes": ["calendar:read"]},
    )
    assert resp.status_code == 400
    assert "already exists" in resp.json()["detail"]

    resp = await client.post(
        "/v1/integrations",
        json={
            "adapter": "github",
            "name": "Secret config",
            "scopes": ["github:read"],
            "config": {"api_key": "do-not-put-here"},
        },
    )
    assert resp.status_code == 400
    assert "credential vault" in resp.json()["detail"]

    health = await install(client, "health", privacy_level="normal")
    assert health["privacy_level"] == "sensitive"  # adapter privacy floor is enforced


async def test_vault_encrypts_and_never_exposes_tokens(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    integration = await install(client, "calendar")
    integration_id = integration["id"]
    token = "super-secret-token-123456"
    await store_oauth(client, integration_id, token=token)

    resp = await client.get(f"/v1/integrations/{integration_id}")
    assert resp.status_code == 200
    assert token not in resp.text
    assert resp.json()["credential_configured"] is True

    resp = await client.get(f"/v1/integrations/{integration_id}/credentials")
    credentials = resp.json()
    assert len(credentials) == 1
    assert token not in resp.text
    assert credentials[0]["configured"] is True
    assert credentials[0]["token_fingerprint_prefix"] == sha256_hex(token)[:8]

    row = (
        await db_session.execute(
            select(IntegrationCredential).where(
                IntegrationCredential.integration_id == UUID(integration_id)
            )
        )
    ).scalar_one()
    assert row.encrypted_access is not None
    assert token not in row.encrypted_access
    assert vault.decrypt(row.encrypted_access) == token
    assert vault.decrypt(row.encrypted_refresh) == "refresh-token-123456"
    assert row.token_fingerprint == sha256_hex(token)

    audit = (
        await db_session.execute(
            select(AccessLog).where(
                AccessLog.action == "integration.credential_store"
            )
        )
    ).scalars().all()
    assert audit
    assert all(token not in json.dumps(row.details) for row in audit)


async def test_action_scope_enforcement(client: AsyncClient) -> None:
    calendar = await install(client, "calendar", scopes=["calendar:read"])
    calendar_id = calendar["id"]
    await store_oauth(client, calendar_id)

    resp = await client.post(
        f"/v1/integrations/{calendar_id}/actions",
        json={"action": "calendar.create_event", "args": {"summary": "Lunch"}},
    )
    assert resp.status_code == 403

    resp = await client.post(
        f"/v1/integrations/{calendar_id}/actions",
        json={"action": "calendar.list_upcoming", "args": {}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"]["ok"] is True

    resp = await client.post(
        f"/v1/integrations/{calendar_id}/actions",
        json={"action": "calendar.not_a_real_action", "args": {}},
    )
    assert resp.status_code == 400

    github = await install(client, "github", scopes=["github:read"])
    github_id = github["id"]
    await store_oauth(client, github_id)
    resp = await client.post(
        f"/v1/integrations/{github_id}/actions",
        json={"action": "github.comment_pr", "args": {"number": 1}},
    )
    assert resp.status_code == 403

    unauthorized = await install(client, "messaging", slug="messaging-unauthed")
    resp = await client.post(
        f"/v1/integrations/{unauthorized['id']}/actions",
        json={"action": "messaging.list_messages", "args": {}},
    )
    assert resp.status_code == 410


async def test_webhook_hmac_ingest_replay_and_privacy(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    calendar = await install(client, "calendar")
    calendar_id = calendar["id"]
    resp = await client.post(f"/v1/integrations/{calendar_id}/webhook-secret")
    assert resp.status_code == 201, resp.text
    secret = resp.json()["secret"]
    assert secret

    payload = {"summary": "Ship review", "start": "2026-08-10T09:00:00Z"}
    result = await webhook(client, calendar_id, payload, secret)
    assert result["accepted"] == 1
    assert result["deduplicated"] == 0
    assert result["channel_id"] == calendar["live_channel_id"]
    event_id = result["event_ids"][0]

    replay = await webhook(client, calendar_id, payload, secret)
    assert replay["accepted"] == 0
    assert replay["deduplicated"] == 1

    resp = await client.get(f"/v1/integrations/{calendar_id}/events")
    assert resp.status_code == 200
    events = resp.json()
    assert len(events) == 1
    assert events[0]["id"] == event_id
    assert events[0]["event_type"] == "calendar.event.updated"
    assert events[0]["payload"]["summary"] == "Ship review"

    # Bad signature and stale timestamp are rejected.
    resp = await client.post(
        f"/v1/integrations/webhook/{calendar_id}",
        content=json.dumps(payload),
        headers={"X-EV-Signature": "sha256=deadbeef", "X-EV-Timestamp": str(int(time.time()))},
    )
    assert resp.status_code == 401
    resp = await client.post(
        f"/v1/integrations/webhook/{calendar_id}",
        content=json.dumps(payload),
        headers=signed_headers(payload, secret, timestamp=int(time.time()) - 3600),
    )
    assert resp.status_code == 401

    # Malformed JSON is rejected.
    raw_body = b"not json"
    ts = str(int(time.time()))
    raw_digest = hmac.new(
        secret.encode("utf-8"),
        f"{ts}.".encode("ascii") + raw_body,
        hashlib.sha256,
    ).hexdigest()
    resp = await client.post(
        f"/v1/integrations/webhook/{calendar_id}",
        content=raw_body,
        headers={
            "X-EV-Signature": f"sha256={raw_digest}",
            "X-EV-Timestamp": ts,
        },
    )
    assert resp.status_code == 400

    # Health adapter enforces its privacy floor on the live channel.
    health = await install(client, "health", privacy_level="normal")
    health_id = health["id"]
    resp = await client.post(f"/v1/integrations/{health_id}/webhook-secret")
    health_secret = resp.json()["secret"]
    result = await webhook(
        client,
        health_id,
        {"metric": "heart_rate", "value": 72, "unit": "bpm"},
        health_secret,
    )
    assert result["accepted"] == 1
    channel = await db_session.get(LiveChannel, UUID(health["live_channel_id"]))
    assert channel.privacy_level == "sensitive"
    event = (
        await db_session.execute(
            select(LiveEvent)
            .where(LiveEvent.channel_id == UUID(health["live_channel_id"]))
            .order_by(LiveEvent.occurred_at.desc())
        )
    ).scalar_one()
    assert event.privacy_level == "sensitive"

    # Webhook secrets are never re-served and are stored encrypted.
    creds = (
        await db_session.execute(
            select(IntegrationCredential).where(
                IntegrationCredential.integration_id == UUID(health_id),
                IntegrationCredential.kind == "webhook_secret",
            )
        )
    ).scalar_one()
    assert health_secret not in creds.encrypted_access
    assert vault.decrypt(creds.encrypted_access) == health_secret


async def test_revocation_is_immediate(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    integration = await install(client, "calendar")
    integration_id = integration["id"]
    await store_oauth(client, integration_id)
    resp = await client.post(f"/v1/integrations/{integration_id}/webhook-secret")
    secret = resp.json()["secret"]
    await webhook(
        client,
        integration_id,
        {"summary": "Before revoke"},
        secret,
    )

    resp = await client.delete(f"/v1/integrations/{integration_id}")
    assert resp.status_code == 200, resp.text
    revoked = resp.json()
    assert revoked["status"] == "revoked"
    assert revoked["revoked_at"] is not None
    assert revoked["credential_configured"] is False
    assert revoked["webhook_configured"] is False

    row = await db_session.get(Integration, UUID(integration_id))
    assert row.status == "revoked"
    credentials = (
        await db_session.execute(
            select(IntegrationCredential).where(
                IntegrationCredential.integration_id == UUID(integration_id)
            )
        )
    ).scalars().all()
    assert all(c.encrypted_access is None and c.token_fingerprint is None for c in credentials)
    channel = await db_session.get(LiveChannel, UUID(integration["live_channel_id"]))
    assert channel.active is False

    resp = await client.post(
        f"/v1/integrations/{integration_id}/actions",
        json={"action": "calendar.list_upcoming", "args": {}},
    )
    assert resp.status_code == 410

    resp = await client.post(
        f"/v1/integrations/webhook/{integration_id}",
        content=json.dumps({"summary": "After revoke"}),
        headers=signed_headers({"summary": "After revoke"}, secret),
    )
    assert resp.status_code == 410

    # History remains readable, but no further ingestion is possible.
    resp = await client.get(f"/v1/integrations/{integration_id}/events")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


async def test_scope_change_revokes_oauth_credential(client: AsyncClient) -> None:
    github = await install(client, "github", scopes=["github:read"])
    github_id = github["id"]
    await store_oauth(client, github_id)

    resp = await client.patch(
        f"/v1/integrations/{github_id}/scopes",
        json={"scopes": ["github:read", "github:act"]},
    )
    assert resp.status_code == 200, resp.text
    assert set(resp.json()["scopes"]) == {"github:read", "github:act"}
    assert resp.json()["credential_configured"] is False

    resp = await client.patch(
        f"/v1/integrations/{github_id}/scopes",
        json={"scopes": ["github:admin"]},
    )
    assert resp.status_code == 403

    resp = await client.patch(
        f"/v1/integrations/{github_id}/scopes",
        json={"scopes": ["github:read"]},
    )
    assert resp.status_code == 200
    assert set(resp.json()["scopes"]) == {"github:read"}


def test_webhook_signature_and_rate_limiter_units() -> None:
    secret = "webhook-secret-123"
    body = b'{"summary": "ok"}'
    headers = signed_headers({"summary": "ok"}, secret)
    verify_webhook_signature(secret=secret, body=body, headers=headers)

    with pytest.raises(SignatureError):
        verify_webhook_signature(secret=secret, body=body, headers={})
    with pytest.raises(SignatureError):
        verify_webhook_signature(secret="wrong", body=body, headers=headers)
    stale = signed_headers({"summary": "ok"}, secret, timestamp=int(time.time()) - 3600)
    with pytest.raises(SignatureError):
        verify_webhook_signature(secret=secret, body=body, headers=stale)

    limiter = SlidingWindowRateLimiter(limit=2, window_seconds=60)
    assert limiter.allow("integration-a")
    assert limiter.allow("integration-a")
    assert limiter.allow("integration-a") is False
    assert limiter.allow("integration-b")


VALID_HANDLER = (
    "values = args.get('values', [])\n"
    "return {'count': len(values), 'total': sum(values)}"
)


def plugin_manifest(
    *,
    slug: str,
    permissions: list[str],
    handler: str = VALID_HANDLER,
    command_permission: str = "memory:read",
) -> dict:
    return {
        "schema_version": "ev.plugin.v1",
        "name": slug.replace("-", " ").title(),
        "slug": slug,
        "version": "1.0.0",
        "description": "test plugin",
        "permissions": permissions,
        "commands": [
            {
                "name": "summarize",
                "description": "summarize values",
                "permission": command_permission,
                "handler": handler,
            }
        ],
    }


async def test_plugin_lifecycle_and_sandbox(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    dangerous = plugin_manifest(
        slug="bad-plugin",
        permissions=["memory:read"],
        handler="import os\nreturn {}",
    )
    resp = await client.post("/v1/plugins", json=dangerous)
    assert resp.status_code == 400

    dangerous_call = plugin_manifest(
        slug="bad-call",
        permissions=["memory:read"],
        handler="return {'x': getattr(args, '__class__')}",
    )
    resp = await client.post("/v1/plugins", json=dangerous_call)
    assert resp.status_code == 400

    manifest = plugin_manifest(slug="sum-helper", permissions=["memory:read"])
    resp = await client.post("/v1/plugins", json=manifest)
    assert resp.status_code == 201, resp.text
    plugin = resp.json()
    plugin_id = plugin["id"]
    assert plugin["status"] == "pending"
    assert plugin["checksum"] == sha256_hex(json.dumps(manifest, sort_keys=True, separators=(",", ":")))

    resp = await client.post(
        f"/v1/plugins/{plugin_id}/commands/summarize",
        json={"args": {"values": [1, 2, 3]}},
    )
    assert resp.status_code == 403

    resp = await client.post("/v1/devices", json={"name": "plugin-test-device"})
    device = resp.json()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {device['token']}"},
    ) as device_client:
        resp = await device_client.post(f"/v1/plugins/{plugin_id}/approve")
        assert resp.status_code == 403

    resp = await client.post(f"/v1/plugins/{plugin_id}/approve")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"

    resp = await client.post(
        f"/v1/plugins/{plugin_id}/commands/summarize",
        json={"args": {"values": [1, 2, 3]}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"] == {"count": 3, "total": 6}

    resp = await client.post(f"/v1/plugins/{plugin_id}/disable")
    assert resp.status_code == 200
    resp = await client.post(
        f"/v1/plugins/{plugin_id}/commands/summarize",
        json={"args": {}},
    )
    assert resp.status_code == 403

    resp = await client.post(f"/v1/plugins/{plugin_id}/enable")
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"

    emit_manifest = plugin_manifest(
        slug="emit-plugin",
        permissions=["live:emit"],
        handler=(
            "return {'emit': [{'event_type': 'plugin.event', "
            "'payload': {'text': 'custom signal'}}]}"
        ),
        command_permission="live:emit",
    )
    resp = await client.post("/v1/plugins", json=emit_manifest)
    assert resp.status_code == 201, resp.text
    emit_plugin_id = resp.json()["id"]
    await client.post(f"/v1/plugins/{emit_plugin_id}/approve")
    resp = await client.post(
        f"/v1/plugins/{emit_plugin_id}/commands/summarize",
        json={"args": {}},
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["emitted_events"]) == 1
    assert resp.json()["emitted_events"][0]["event_type"] == "plugin.event"

    row = await db_session.get(Plugin, UUID(plugin_id))
    assert row.status == "approved"


async def test_plugin_context_respects_permissions(client: AsyncClient) -> None:
    manifest = plugin_manifest(
        slug="context-plugin",
        permissions=["memory:read"],
        handler="return {'has_memories': 'memories' in context, 'memory_count': len(context.get('memories', []))}",
    )
    resp = await client.post("/v1/plugins", json=manifest)
    plugin_id = resp.json()["id"]
    await client.post(f"/v1/plugins/{plugin_id}/approve")
    resp = await client.post(
        f"/v1/plugins/{plugin_id}/commands/summarize",
        json={"args": {}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"]["has_memories"] is True
