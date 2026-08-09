"""Integration lifecycle: install, credentials, scopes, actions, revocation.

Security invariants enforced here:

- Every integration is isolated with its own scopes, privacy scope, live
  channel, credentials, and webhook secret.
- Scopes are always validated against the adapter's declared capabilities
  (least privilege; unknown scopes are rejected).
- Credentials are stored encrypted (see :mod:`app.integrations.vault`);
  plaintext never leaves this module's call stack, is never logged, and is
  never included in model context.
- Revocation is immediate: status -> revoked, credentials wiped, live channel
  deactivated, and every gate fails closed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.ev.live import create_channel, ingest_events, list_events
from app.integrations import vault, webhooks
from app.integrations.adapters import registry
from app.models import (
    Integration,
    IntegrationCredential,
    LiveChannel,
    LiveEvent,
    WebhookDelivery,
)
from app.schemas import (
    IntegrationActionOut,
    IntegrationCatalogItem,
    IntegrationCreate,
    IntegrationCredentialCreate,
    IntegrationCredentialOut,
    IntegrationOut,
    IntegrationScopeUpdate,
    LiveChannelCreate,
    WebhookIngestOut,
    WebhookSecretOut,
)
from app.services.access_log import log_access
from app.utils.text import utcnow

PRIVACY_ORDER = {
    "normal": 0,
    "sensitive": 1,
    "private": 2,
    "never_send_to_model": 3,
}

SECRET_CONFIG_MARKERS = (
    "token",
    "secret",
    "password",
    "apikey",
    "api_key",
    "authorization",
    "credential",
    "client_secret",
    "private_key",
)

LiveChannelKind = Literal["screen", "audio", "health", "app", "vision", "location"]
PrivacyLevelLiteral = Literal["private", "normal", "sensitive", "never_send_to_model"]


def _privacy_order(value: str) -> int:
    return PRIVACY_ORDER.get(value, 0)


def _validate_config(config: dict) -> None:
    offending = [
        key
        for key in config
        if any(marker in str(key).lower() for marker in SECRET_CONFIG_MARKERS)
    ]
    if offending:
        raise ValueError(
            "integration config must not contain credentials "
            f"(offending keys: {sorted(offending)}); use the credential vault instead"
        )


def _webhook_occurred_at(headers: dict) -> datetime | None:
    timestamp = webhooks.header_value(headers, "X-EV-Timestamp")
    if not timestamp:
        return None
    try:
        return datetime.fromtimestamp(float(timestamp), tz=UTC)
    except (TypeError, ValueError):
        return None


def _integration_out(row: Integration, credentials: list[IntegrationCredential]) -> IntegrationOut:
    active_kinds = {c.kind for c in credentials if c.revoked_at is None}
    return IntegrationOut(
        id=row.id,
        slug=row.slug,
        adapter=row.adapter,
        name=row.name,
        scopes=row.scopes or [],
        status=row.status,
        privacy_level=row.privacy_level,
        config=row.config or {},
        live_channel_id=row.live_channel_id,
        credential_configured="oauth" in active_kinds,
        webhook_configured="webhook_secret" in active_kinds,
        last_used_at=row.last_used_at,
        last_webhook_at=row.last_webhook_at,
        revoked_at=row.revoked_at,
        revoked_reason=row.revoked_reason,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _credential_out(row: IntegrationCredential) -> IntegrationCredentialOut:
    return IntegrationCredentialOut(
        kind=row.kind,
        configured=row.encrypted_access is not None and row.revoked_at is None,
        scopes=row.scopes or [],
        provider_account_id=row.provider_account_id,
        token_fingerprint_prefix=(row.token_fingerprint or "")[:8],
        expires_at=row.expires_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _credentials_for(
    session: AsyncSession,
    integration_id: UUID,
) -> list[IntegrationCredential]:
    rows = (
        await session.execute(
            select(IntegrationCredential).where(
                IntegrationCredential.integration_id == integration_id
            )
        )
    ).scalars().all()
    return list(rows)


async def _credential(
    session: AsyncSession,
    integration_id: UUID,
    kind: str,
) -> IntegrationCredential | None:
    return (
        await session.execute(
            select(IntegrationCredential).where(
                IntegrationCredential.integration_id == integration_id,
                IntegrationCredential.kind == kind,
            )
        )
    ).scalar_one_or_none()


async def _active_integration(session: AsyncSession, integration_id: UUID) -> Integration:
    row = await session.get(Integration, integration_id)
    if row is None:
        raise KeyError(integration_id)
    if row.status != "active":
        raise LookupError("integration is revoked")
    return row


def catalog() -> list[IntegrationCatalogItem]:
    return [
        IntegrationCatalogItem(
            adapter=adapter.slug,
            name=adapter.name,
            description=adapter.description,
            capabilities=list(adapter.capabilities),
            default_scopes=list(adapter.default_scopes),
            min_privacy=adapter.min_privacy,
            privacy_kind=adapter.privacy_kind,
            event_types=list(adapter.event_types),
            actions=[
                {
                    "name": action.name,
                    "scope": action.scope,
                    "description": action.description,
                }
                for action in adapter.actions
            ],
        )
        for adapter in registry.all()
    ]


async def install(
    session: AsyncSession,
    data: IntegrationCreate,
    actor: str,
) -> Integration:
    adapter = registry.get(data.adapter)
    if adapter is None:
        raise ValueError(f"unknown adapter '{data.adapter}'")
    if not adapter.scopes_ok(data.scopes):
        raise PermissionError(
            f"scopes must be a non-empty subset of {sorted(adapter.capabilities)}"
        )
    _validate_config(data.config)
    privacy: str = data.privacy_level
    if _privacy_order(privacy) < _privacy_order(adapter.min_privacy):
        privacy = adapter.min_privacy
    slug = data.slug or adapter.slug
    existing = (
        await session.execute(select(Integration).where(Integration.slug == slug))
    ).scalar_one_or_none()
    if existing is not None:
        if existing.status == "active":
            raise ValueError(f"integration slug '{slug}' already exists")
        channel = await create_channel(
            session,
            LiveChannelCreate(
                name=f"integration:{slug}",
                kind=cast(LiveChannelKind, adapter.privacy_kind),
                privacy_level=cast(PrivacyLevelLiteral, privacy),
                metadata={"collector": f"integration:{slug}", "adapter": adapter.slug},
            ),
        )
        existing.adapter = adapter.slug
        existing.name = data.name
        existing.scopes = sorted(set(data.scopes))
        existing.privacy_level = privacy
        existing.config = data.config
        existing.live_channel_id = channel.id
        existing.status = "active"
        existing.revoked_at = None
        existing.revoked_reason = None
        existing.last_used_at = None
        existing.last_webhook_at = None
        await log_access(
            session,
            actor=actor,
            action="integration.reinstall",
            endpoint="POST /v1/integrations",
            resource_type="integration",
            resource_ids=[existing.id],
            details={
                "adapter": adapter.slug,
                "scopes": sorted(set(data.scopes)),
                "privacy_level": privacy,
            },
        )
        return existing
    channel = await create_channel(
        session,
        LiveChannelCreate(
            name=f"integration:{slug}",
            kind=cast(LiveChannelKind, adapter.privacy_kind),
            privacy_level=cast(PrivacyLevelLiteral, privacy),
            metadata={"collector": f"integration:{slug}", "adapter": adapter.slug},
        ),
    )
    row = Integration(
        slug=slug,
        adapter=adapter.slug,
        name=data.name,
        scopes=sorted(set(data.scopes)),
        status="active",
        privacy_level=privacy,
        config=data.config,
        live_channel_id=channel.id,
    )
    session.add(row)
    await session.flush()
    await log_access(
        session,
        actor=actor,
        action="integration.install",
        endpoint="POST /v1/integrations",
        resource_type="integration",
        resource_ids=[row.id],
        details={
            "adapter": adapter.slug,
            "scopes": sorted(set(data.scopes)),
            "privacy_level": privacy,
        },
    )
    return row


async def list_integrations(
    session: AsyncSession,
    *,
    include_revoked: bool = False,
) -> list[IntegrationOut]:
    stmt = select(Integration).order_by(Integration.created_at.asc())
    if not include_revoked:
        stmt = stmt.where(Integration.status == "active")
    rows = list((await session.execute(stmt)).scalars().all())
    credentials = list((await session.execute(select(IntegrationCredential))).scalars().all())
    by_integration: dict[UUID, list[IntegrationCredential]] = {}
    for credential in credentials:
        by_integration.setdefault(credential.integration_id, []).append(credential)
    return [
        _integration_out(row, by_integration.get(row.id, []))
        for row in rows
    ]


async def get_integration(session: AsyncSession, integration_id: UUID) -> IntegrationOut:
    row = await session.get(Integration, integration_id)
    if row is None:
        raise KeyError(integration_id)
    return _integration_out(row, await _credentials_for(session, integration_id))


async def list_credentials(
    session: AsyncSession,
    integration_id: UUID,
) -> list[IntegrationCredentialOut]:
    row = await session.get(Integration, integration_id)
    if row is None:
        raise KeyError(integration_id)
    return [
        _credential_out(credential)
        for credential in await _credentials_for(session, integration_id)
    ]


async def store_oauth(
    session: AsyncSession,
    integration_id: UUID,
    data: IntegrationCredentialCreate,
    actor: str,
) -> IntegrationCredentialOut:
    integration = await _active_integration(session, integration_id)
    adapter = registry.get(integration.adapter)
    if adapter is None:
        raise LookupError(f"adapter '{integration.adapter}' is unavailable")
    scopes = data.scopes if data.scopes is not None else (integration.scopes or [])
    if not adapter.scopes_ok(scopes) or not set(scopes) <= set(integration.scopes or []):
        raise PermissionError(
            "credential scopes must be a non-empty subset of the integration's granted scopes"
        )
    row = await _credential(session, integration_id, "oauth")
    if row is None:
        row = IntegrationCredential(integration_id=integration_id, kind="oauth")
        session.add(row)
    row.provider_account_id = data.provider_account_id
    row.scopes = sorted(set(scopes))
    row.encrypted_access = vault.encrypt(data.access_token)
    row.encrypted_refresh = vault.encrypt(data.refresh_token) if data.refresh_token else None
    row.token_type = data.token_type
    row.token_fingerprint = vault.fingerprint(data.access_token)
    row.expires_at = data.expires_at
    row.revoked_at = None
    row.metadata_ = {}
    await session.flush()
    await log_access(
        session,
        actor=actor,
        action="integration.credential_store",
        endpoint="POST /v1/integrations/{id}/credentials",
        resource_type="integration_credential",
        resource_ids=[row.id],
        details={
            "kind": "oauth",
            "provider_account_id": data.provider_account_id,
            "scope_count": len(scopes),
        },
    )
    return _credential_out(row)


async def create_webhook_secret(
    session: AsyncSession,
    integration_id: UUID,
    actor: str,
) -> WebhookSecretOut:
    integration = await _active_integration(session, integration_id)
    secret = vault.new_secret()
    row = await _credential(session, integration_id, "webhook_secret")
    if row is None:
        row = IntegrationCredential(integration_id=integration_id, kind="webhook_secret")
        session.add(row)
    row.encrypted_access = vault.encrypt(secret)
    row.encrypted_refresh = None
    row.token_fingerprint = vault.fingerprint(secret)
    row.scopes = []
    row.revoked_at = None
    row.metadata_ = {"rotated_at": utcnow().isoformat()}
    await session.flush()
    await log_access(
        session,
        actor=actor,
        action="integration.webhook_secret_rotate",
        endpoint="POST /v1/integrations/{id}/webhook-secret",
        resource_type="integration_credential",
        resource_ids=[row.id],
        details={"adapter": integration.adapter},
    )
    return WebhookSecretOut(configured=True, rotated_at=utcnow(), secret=secret)


async def _webhook_secret(
    session: AsyncSession,
    integration_id: UUID,
) -> str:
    row = await _credential(session, integration_id, "webhook_secret")
    if row is None or row.revoked_at is not None or not row.encrypted_access:
        raise LookupError("webhook secret is not configured")
    return vault.decrypt(row.encrypted_access)


def _parse_expires(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


async def refresh_oauth(
    session: AsyncSession,
    integration_id: UUID,
    actor: str,
) -> IntegrationCredentialOut:
    """Refresh an OAuth access token through the adapter's refresh flow."""
    integration = await _active_integration(session, integration_id)
    adapter = registry.get(integration.adapter)
    if adapter is None:
        raise LookupError(f"adapter '{integration.adapter}' is unavailable")
    credential = await _credential(session, integration_id, "oauth")
    if (
        credential is None
        or credential.revoked_at is not None
        or not credential.encrypted_access
        or not credential.encrypted_refresh
    ):
        raise LookupError("integration has no refreshable OAuth credential")
    token = vault.decrypt(credential.encrypted_access)
    refresh = vault.decrypt(credential.encrypted_refresh)
    try:
        try:
            outcome = await adapter.refresh_token(
                token=token,
                refresh_token=refresh,
                config=integration.config or {},
            )
        except NotImplementedError as exc:
            raise ValueError(str(exc)) from exc
    finally:
        del token, refresh
    access_token = outcome.get("access_token")
    if not isinstance(access_token, str) or len(access_token) < 8:
        raise ValueError("adapter refresh flow returned an invalid access token")
    credential.encrypted_access = vault.encrypt(access_token)
    new_refresh = outcome.get("refresh_token")
    if isinstance(new_refresh, str) and len(new_refresh) >= 8:
        credential.encrypted_refresh = vault.encrypt(new_refresh)
    token_type = outcome.get("token_type")
    if isinstance(token_type, str):
        credential.token_type = token_type
    expires = _parse_expires(outcome.get("expires_at"))
    if expires is not None:
        credential.expires_at = expires
    credential.token_fingerprint = vault.fingerprint(access_token)
    credential.revoked_at = None
    integration.last_used_at = utcnow()
    await log_access(
        session,
        actor=actor,
        action="integration.credential_refresh",
        endpoint="POST /v1/integrations/{id}/credentials/refresh",
        resource_type="integration_credential",
        resource_ids=[credential.id],
        details={"adapter": integration.adapter, "rotated": True},
    )
    return _credential_out(credential)


async def update_scopes(
    session: AsyncSession,
    integration_id: UUID,
    data: IntegrationScopeUpdate,
    actor: str,
) -> IntegrationOut:
    integration = await _active_integration(session, integration_id)
    adapter = registry.get(integration.adapter)
    if adapter is None:
        raise LookupError(f"adapter '{integration.adapter}' is unavailable")
    if not adapter.scopes_ok(data.scopes):
        raise PermissionError(
            f"scopes must be a non-empty subset of {sorted(adapter.capabilities)}"
        )
    oauth = await _credential(session, integration_id, "oauth")
    changed = set(data.scopes) != set(integration.scopes or [])
    if changed:
        if oauth is not None and oauth.revoked_at is None:
            oauth.revoked_at = utcnow()
            oauth.encrypted_access = None
            oauth.encrypted_refresh = None
            oauth.token_fingerprint = None
            oauth.scopes = []
        integration.scopes = sorted(set(data.scopes))
        await log_access(
            session,
            actor=actor,
            action="integration.scope_change",
            endpoint="PATCH /v1/integrations/{id}/scopes",
            resource_type="integration",
            resource_ids=[integration.id],
            details={
                "adapter": integration.adapter,
                "scopes": integration.scopes,
                "oauth_credential_revoked": oauth is not None,
            },
        )
    return _integration_out(integration, await _credentials_for(session, integration_id))


async def revoke(
    session: AsyncSession,
    integration_id: UUID,
    actor: str,
    reason: str = "user revoked",
) -> IntegrationOut:
    row = await session.get(Integration, integration_id)
    if row is None:
        raise KeyError(integration_id)
    row.status = "revoked"
    row.revoked_at = utcnow()
    row.revoked_reason = reason
    remote_revocation: dict | None = None
    remote_revocation_error: str | None = None
    if row.config.get("revoke_remote"):
        oauth = await _credential(session, integration_id, "oauth")
        if (
            oauth is not None
            and oauth.revoked_at is None
            and oauth.encrypted_access is not None
        ):
            token = vault.decrypt(oauth.encrypted_access)
            adapter = registry.get(row.adapter)
            try:
                if adapter is not None:
                    remote_result = await adapter.revoke_remote(
                        token=token,
                        config=row.config or {},
                    )
                    remote_revocation = {
                        "ok": bool(remote_result.get("ok", True)),
                        "mode": remote_result.get("mode", "provider"),
                    }
            except Exception as exc:  # noqa: BLE001 - best effort, local revoke proceeds
                remote_revocation_error = f"{type(exc).__name__}: {exc}"
            finally:
                del token
    credentials = await _credentials_for(session, integration_id)
    for credential in credentials:
        credential.revoked_at = utcnow()
        credential.encrypted_access = None
        credential.encrypted_refresh = None
        credential.token_fingerprint = None
        credential.scopes = []
    if row.live_channel_id is not None:
        channel = await session.get(LiveChannel, row.live_channel_id)
        if channel is not None:
            channel.active = False
    await log_access(
        session,
        actor=actor,
        action="integration.revoke",
        endpoint="DELETE /v1/integrations/{id}",
        resource_type="integration",
        resource_ids=[row.id],
        details={
            "adapter": row.adapter,
            "reason": reason,
            "remote_revocation": remote_revocation,
            "remote_revocation_error": remote_revocation_error,
        },
    )
    return _integration_out(row, credentials)


async def rotate_vault(
    session: AsyncSession,
    *,
    new_key: str,
    actor: str,
) -> dict:
    """Re-encrypt every vaulted credential under a new vault key (master-only).

    The rotation decrypts each ciphertext with the current key and immediately
    re-encrypts it with the new key, then swaps the active vault key. Plaintext
    exists only in memory for one credential at a time; nothing is logged.
    """
    if len(new_key) < 16:
        raise ValueError("vault key must be at least 16 characters")
    credentials = (
        await session.execute(select(IntegrationCredential))
    ).scalars().all()
    reencrypted = 0
    for credential in credentials:
        changed = False
        if credential.encrypted_access:
            plaintext = vault.decrypt(credential.encrypted_access)
            credential.encrypted_access = vault.encrypt_with(new_key, plaintext)
            changed = True
        if credential.encrypted_refresh:
            plaintext = vault.decrypt(credential.encrypted_refresh)
            credential.encrypted_refresh = vault.encrypt_with(new_key, plaintext)
            changed = True
        if changed:
            reencrypted += 1
    settings.vault_key = new_key
    vault.reset()
    await log_access(
        session,
        actor=actor,
        action="vault.rotate",
        endpoint="POST /v1/integrations/vault/rotate",
        resource_type="integration_credential",
        resource_ids=[],
        details={"reencrypted_credentials": reencrypted},
    )
    return {"rotated": True, "reencrypted_credentials": reencrypted}


async def execute_action(
    session: AsyncSession,
    integration_id: UUID,
    action: str,
    args: dict,
    actor: str,
) -> IntegrationActionOut:
    integration = await _active_integration(session, integration_id)
    adapter = registry.get(integration.adapter)
    if adapter is None:
        raise LookupError(f"adapter '{integration.adapter}' is unavailable")
    spec = adapter.action(action)
    if spec is None:
        raise ValueError(f"adapter '{adapter.slug}' has no action '{action}'")
    if spec.scope not in (integration.scopes or []):
        raise PermissionError(f"scope '{spec.scope}' is not granted")
    credential = await _credential(session, integration_id, "oauth")
    if (
        credential is None
        or credential.revoked_at is not None
        or not credential.encrypted_access
    ):
        raise LookupError("integration is not authorized with OAuth credentials")
    token = vault.decrypt(credential.encrypted_access)
    try:
        result = await adapter.act(
            action=action,
            args=args or {},
            token=token,
            scopes=list(integration.scopes or []),
            config=integration.config or {},
        )
    finally:
        del token  # minimize plaintext lifetime
    integration.last_used_at = utcnow()
    await log_access(
        session,
        actor=actor,
        action="integration.act",
        endpoint="POST /v1/integrations/{id}/actions",
        resource_type="integration",
        resource_ids=[integration.id],
        details={
            "adapter": adapter.slug,
            "action": action,
            "scope": spec.scope,
            "provider": (integration.config or {}).get("provider", "local"),
        },
    )
    return IntegrationActionOut(
        adapter=adapter.slug,
        action=action,
        result=result,
        executed_at=utcnow(),
    )


async def ingest_webhook(
    session: AsyncSession,
    integration_id: UUID,
    body: bytes,
    headers: dict,
) -> WebhookIngestOut:
    integration = await _active_integration(session, integration_id)
    adapter = registry.get(integration.adapter)
    if adapter is None:
        raise LookupError(f"adapter '{integration.adapter}' is unavailable")
    secret = await _webhook_secret(session, integration_id)
    webhooks.verify_webhook_signature(secret=secret, body=body, headers=headers)
    delivery_key = webhooks.header_value(headers, "X-EV-Delivery-Id")
    if not delivery_key:
        # Providers without delivery ids get content-fingerprint replay
        # protection: the same signed body (regardless of header timestamp)
        # is idempotent.
        delivery_key = "content:" + sha256_hex(body.decode("utf-8", "replace"))
    if len(delivery_key) > 160 or not delivery_key.strip():
        raise ValueError("X-EV-Delivery-Id must be 1..128 non-blank characters")
    prior = (
        await session.execute(
            select(WebhookDelivery).where(
                WebhookDelivery.integration_id == integration.id,
                WebhookDelivery.delivery_key == delivery_key,
            )
        )
    ).scalar_one_or_none()
    if prior is not None:
        return WebhookIngestOut(
            integration_id=integration.id,
            adapter=adapter.slug,
            accepted=0,
            deduplicated=prior.event_count,
            channel_id=integration.live_channel_id,
            event_ids=[UUID(value) for value in (prior.event_ids or [])],
        )
    if not webhooks.webhook_rate_limiter.allow(str(integration_id)):
        raise webhooks.RateLimitError("webhook rate limit exceeded")
    try:
        payload = json.loads(body.decode("utf-8") or b"{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("webhook body must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("webhook body must be a JSON object")
    events = await adapter.translate_webhook(payload, headers)
    if not events:
        return WebhookIngestOut(
            integration_id=integration.id,
            adapter=adapter.slug,
            accepted=0,
            deduplicated=0,
            channel_id=integration.live_channel_id,
            event_ids=[],
        )
    occurred_at = _webhook_occurred_at(headers)
    if occurred_at is not None:
        events = [
            event
            if event.occurred_at is not None
            else event.model_copy(update={"occurred_at": occurred_at})
            for event in events
        ]
    channel = await session.get(LiveChannel, integration.live_channel_id)
    if channel is None or not channel.active:
        raise LookupError("integration live channel is inactive")
    stored = await ingest_events(session, channel, events)
    integration.last_webhook_at = utcnow()
    if delivery_key:
        session.add(
            WebhookDelivery(
                integration_id=integration.id,
                delivery_key=delivery_key,
                event_ids=[str(event.id) for event in stored],
                event_count=len(events),
            )
        )
    await log_access(
        session,
        actor=f"integration:{integration.slug}",
        action="integration.webhook",
        endpoint="POST /v1/integrations/webhook/{id}",
        resource_type="live_event",
        resource_ids=[event.id for event in stored],
        details={
            "adapter": adapter.slug,
            "accepted": len(stored),
            "deduplicated": len(events) - len(stored),
            "event_types": sorted({event.event_type for event in events}),
        },
    )
    return WebhookIngestOut(
        integration_id=integration.id,
        adapter=adapter.slug,
        accepted=len(stored),
        deduplicated=len(events) - len(stored),
        channel_id=channel.id,
        event_ids=[event.id for event in stored],
    )


async def integration_events(
    session: AsyncSession,
    integration_id: UUID,
    *,
    limit: int = 100,
) -> list[LiveEvent]:
    integration = await session.get(Integration, integration_id)
    if integration is None:
        raise KeyError(integration_id)
    if integration.live_channel_id is None:
        return []
    return await list_events(session, integration.live_channel_id, limit=limit)
