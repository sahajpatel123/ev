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
import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.ev.live import create_channel, ingest_events, list_events
from app.gateway.validation import validate_arguments
from app.integrations import oauth, vault, webhooks
from app.integrations.adapters import registry
from app.integrations.calendar_signals import derive_calendar_signals
from app.integrations.life_helper import LifeHelperError
from app.integrations.life_policy import evaluate_life_policy
from app.models import (
    Integration,
    IntegrationCredential,
    LifeOutboundAction,
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
    IntegrationSyncOut,
    LifeDeviceResultIn,
    LifeDeviceResultOut,
    LifeOutboxEntryOut,
    LifeOutboxOut,
    LifePolicyOut,
    LiveChannelCreate,
    LiveEventCreate,
    OAuthAuthorizeOut,
    OAuthStatusOut,
    WebhookIngestOut,
    WebhookSecretOut,
)
from app.services.access_log import log_access
from app.utils.text import sha256_hex, utcnow

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


def _short_text(value: object, limit: int) -> str:
    if isinstance(value, str):
        return value[:limit]
    if value is None:
        return ""
    return str(value)[:limit]


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
        if credential.kind != "oauth_state"  # transient PKCE flow state, never exposed
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
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _as_aware(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; normalize for comparisons."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


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
        except oauth.OAuthReauthRequiredError:
            credential.metadata_ = {
                **(credential.metadata_ or {}),
                "reauth_required": True,
            }
            # Persist the re-auth flag even though the caller aborts: the next
            # status check must report that the grant is dead.
            await session.commit()
            raise
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


async def begin_oauth_flow(
    session: AsyncSession,
    integration_id: UUID,
    actor: str,
) -> OAuthAuthorizeOut:
    """Start an authorization-code + PKCE flow and store the verifier in the vault."""
    integration = await _active_integration(session, integration_id)
    provider = oauth.provider_for(integration.adapter)
    if provider is None:
        raise ValueError(f"adapter '{integration.adapter}' has no OAuth provider")
    missing = provider.missing_credentials()
    if missing:
        raise ValueError(
            "OAuth provider is not configured: missing "
            + ", ".join(missing)
            + " (see docs/INTEGRATIONS.md)"
        )
    state = secrets.token_urlsafe(32)
    verifier, _ = oauth.new_pkce_pair()
    expires_at = utcnow() + timedelta(seconds=settings.oauth_state_ttl_seconds)
    row = await _credential(session, integration.id, "oauth_state")
    if row is None:
        row = IntegrationCredential(integration_id=integration.id, kind="oauth_state")
        session.add(row)
    # The code verifier and CSRF state are vault-encrypted; only an expiry is
    # plaintext, and the row is deleted as soon as the callback completes.
    row.encrypted_access = vault.encrypt(verifier)
    row.encrypted_refresh = vault.encrypt(state)
    row.token_fingerprint = vault.fingerprint(state)
    row.scopes = []
    row.token_type = None
    row.revoked_at = None
    row.metadata_ = {"expires_at": expires_at.isoformat()}
    await session.flush()
    await log_access(
        session,
        actor=actor,
        action="integration.oauth_authorize",
        endpoint="GET /v1/integrations/oauth/authorize",
        resource_type="integration",
        resource_ids=[integration.id],
        details={
            "adapter": integration.adapter,
            "provider": provider.slug,
            "expires_at": expires_at.isoformat(),
        },
    )
    return OAuthAuthorizeOut(
        authorize_url=provider.build_authorize_url(state=state, code_verifier=verifier),
        state=state,
        expires_at=expires_at,
    )


async def complete_oauth_flow(
    session: AsyncSession,
    *,
    state: str,
    code: str,
    actor: str,
) -> IntegrationCredentialOut:
    """Exchange an authorization code (PKCE) and store the tokens in the vault."""
    if not state or not code:
        raise ValueError("OAuth callback requires state and code")
    rows = (
        await session.execute(
            select(IntegrationCredential).where(
                IntegrationCredential.kind == "oauth_state"
            )
        )
    ).scalars().all()
    state_row = None
    for row in rows:
        if row.encrypted_refresh and vault.decrypt(row.encrypted_refresh) == state:
            state_row = row
            break
    if state_row is None:
        raise ValueError("OAuth state is unknown or already used")
    expires_at = _parse_expires((state_row.metadata_ or {}).get("expires_at"))
    if expires_at is None or expires_at < utcnow():
        raise ValueError("OAuth authorization expired; start a new one")
    if not state_row.encrypted_access:
        raise ValueError("OAuth state is incomplete; start a new authorization")
    verifier = vault.decrypt(state_row.encrypted_access)
    integration = await session.get(Integration, state_row.integration_id)
    if integration is None or integration.status != "active":
        raise LookupError("integration is revoked")
    adapter = registry.get(integration.adapter)
    provider = oauth.provider_for(integration.adapter)
    if adapter is None or provider is None:
        raise LookupError(f"adapter '{integration.adapter}' is unavailable")
    outcome = await provider.exchange_code(code, verifier)
    account_email = oauth.id_token_email(outcome)
    oauth_row = await _credential(session, integration.id, "oauth")
    if oauth_row is None:
        oauth_row = IntegrationCredential(integration_id=integration.id, kind="oauth")
        session.add(oauth_row)
    oauth_row.provider_account_id = account_email
    oauth_row.scopes = sorted(set(integration.scopes or []))
    oauth_row.encrypted_access = vault.encrypt(outcome["access_token"])
    refresh_token = outcome.get("refresh_token")
    if isinstance(refresh_token, str):
        oauth_row.encrypted_refresh = vault.encrypt(refresh_token)
    elif not oauth_row.encrypted_refresh:
        oauth_row.encrypted_refresh = None
    oauth_row.token_type = outcome.get("token_type") or "Bearer"
    oauth_row.token_fingerprint = vault.fingerprint(outcome["access_token"])
    oauth_row.expires_at = outcome.get("expires_at")
    oauth_row.revoked_at = None
    oauth_row.metadata_ = {
        "provider": provider.slug,
        "auth_method": "authorization_code_pkce",
    }
    await session.delete(state_row)
    await session.flush()
    await log_access(
        session,
        actor=actor,
        action="integration.oauth_callback",
        endpoint="GET /v1/integrations/oauth/callback",
        resource_type="integration",
        resource_ids=[integration.id],
        details={
            "adapter": integration.adapter,
            "provider": provider.slug,
            "auth_method": "authorization_code_pkce",
            "provider_account_id": account_email,
        },
    )
    return _credential_out(oauth_row)


async def oauth_status(
    session: AsyncSession,
    integration_id: UUID,
) -> OAuthStatusOut:
    integration = await session.get(Integration, integration_id)
    if integration is None:
        raise KeyError(integration_id)
    provider = oauth.provider_for(integration.adapter)
    credential = await _credential(session, integration_id, "oauth")
    configured = (
        credential is not None
        and credential.revoked_at is None
        and bool(credential.encrypted_access)
    )
    expires_at = _as_aware(credential.expires_at) if credential is not None else None
    expired = expires_at is not None and expires_at <= utcnow()
    has_refresh = credential is not None and bool(credential.encrypted_refresh)
    reauth_required = (
        not configured
        or (expired and not has_refresh)
        or bool((credential.metadata_ if credential is not None else {}).get("reauth_required"))
    )
    return OAuthStatusOut(
        provider=provider.slug if provider is not None else None,
        authorized=configured and not reauth_required,
        configured=configured,
        expires_at=expires_at,
        expired=expired,
        reauth_required=reauth_required,
        provider_account_id=credential.provider_account_id if credential else None,
        scopes=list(integration.scopes or []),
    )


async def sync_integration(
    session: AsyncSession,
    integration_id: UUID,
    actor: str,
    *,
    days: int | None = None,
    device_id=None,
) -> IntegrationSyncOut:
    """Pull provider data into the integration's live channel (idempotent)."""
    integration = await _active_integration(session, integration_id)
    adapter = registry.get(integration.adapter)
    if adapter is None:
        raise LookupError(f"adapter '{integration.adapter}' is unavailable")
    if integration.live_channel_id is None:
        raise LookupError("integration has no live channel")
    from app.ev.policy import authorize
    from app.ev.tools import get_spec

    sync_name = {
        "calendar": "calendar_read",
        "messaging": "list_messages",
        "mail": "list_mail",
        "contacts": "resolve_contact",
    }.get(integration.adapter)
    sync_spec = get_spec(sync_name) if sync_name else {
        "name": f"{integration.adapter}.sync",
        "description": f"Read from the {integration.adapter} provider",
        "parameters": {"type": "object", "additionalProperties": False},
        "output": {"type": "object"},
        "permission": f"{integration.adapter}:read",
        "read_only": True,
        "sensitive": False,
        "risk_class": "R0",
        "confirmation": "none",
        "target_ownership": "owner",
        "provider": integration.adapter,
        "evidence": ["source", "timestamp"],
    }
    decision = await authorize(
        session,
        sync_name or f"{integration.adapter}.sync",
        actor=actor,
        arguments={},
        device_id=device_id,
        channel="action",
        spec=sync_spec,
        provider_scopes_override=list(integration.scopes or []),
        provider_connected_override=True,
    )
    if not decision.allowed:
        raise PermissionError(decision.reason)
    credential = await _credential(session, integration_id, "oauth")
    if (
        credential is None
        or credential.revoked_at is not None
        or not credential.encrypted_access
    ):
        raise LookupError("integration is not authorized with OAuth credentials")
    now = utcnow()
    since = now - timedelta(hours=1)
    until = now + timedelta(days=days or settings.calendar_sync_days)
    token = vault.decrypt(credential.encrypted_access)
    sync_config = dict(integration.config or {})
    sync_config["_session"] = session
    try:
        try:
            result = await adapter.sync(
                token=token,
                scopes=list(integration.scopes or []),
                config=sync_config,
                since=since,
                until=until,
            )
        except oauth.OAuthAuthError:
            await refresh_oauth(session, integration_id, actor)
            credential = await _credential(session, integration_id, "oauth")
            if credential is None or not credential.encrypted_access:
                raise oauth.OAuthReauthRequiredError(
                    "integration requires re-authorization after a failed refresh"
                ) from None
            token = vault.decrypt(credential.encrypted_access)
            result = await adapter.sync(
                token=token,
                scopes=list(integration.scopes or []),
                config=sync_config,
                since=since,
                until=until,
            )
    finally:
        del token
    channel = await session.get(LiveChannel, integration.live_channel_id)
    if channel is None or not channel.active:
        raise LookupError("integration live channel is inactive")
    existing_ids: set[str] = set()
    for row in await list_events(session, channel.id, limit=500):
        event_id = (row.payload or {}).get("event_id")
        if isinstance(event_id, str) and event_id:
            existing_ids.add(event_id)
    fresh = [
        event
        for event in result.events
        if not (
            isinstance(event.payload.get("event_id"), str)
            and event.payload["event_id"] in existing_ids
        )
    ]
    stored = await ingest_events(session, channel, fresh)
    integration.last_used_at = utcnow()
    await log_access(
        session,
        actor=actor,
        action="integration.sync",
        endpoint="POST /v1/integrations/{id}/sync",
        resource_type="live_event",
        resource_ids=[event.id for event in stored],
        details={
            "adapter": integration.adapter,
            "provider": (integration.config or {}).get("provider", "local"),
            "requested": len(result.events),
            "accepted": len(stored),
            "deduplicated": len(result.events) - len(stored),
            "signal_keys": sorted(result.signals.keys()),
        },
    )
    return IntegrationSyncOut(
        integration_id=integration.id,
        adapter=integration.adapter,
        synced_at=utcnow(),
        accepted=len(stored),
        deduplicated=len(result.events) - len(stored),
        event_count=len(result.events),
        signals=result.signals,
    )


async def calendar_signals(
    session: AsyncSession,
    integration_id: UUID,
) -> dict:
    """Derive calendar signals from stored live events (no provider round trip)."""
    integration = await session.get(Integration, integration_id)
    if integration is None:
        raise KeyError(integration_id)
    if integration.adapter != "calendar":
        raise ValueError("integration is not a calendar adapter")
    if integration.live_channel_id is None:
        return derive_calendar_signals([])
    rows = await list_events(session, integration.live_channel_id, limit=500)
    payloads = [
        row.payload
        for row in rows
        if row.event_type == "calendar.event.updated" and isinstance(row.payload, dict)
    ]
    return derive_calendar_signals(payloads)


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


def _integration_policy_spec(adapter_slug: str, action: str, adapter_spec) -> tuple[str, dict]:
    """Map an adapter action onto a policy-shaped capability contract."""

    from app.ev.policy import RiskClass, confirmation_policy_for

    adapter_name = str(adapter_slug or "integration")
    action_name = str(action or "")
    lowered = action_name.lower()
    if adapter_name == "calendar":
        capability_name = "calendar_read" if any(
            token in lowered for token in ("read", "list", "availability", "freebusy")
        ) else "calendar_add"
    elif adapter_name == "messaging":
        capability_name = "list_messages" if any(
            token in lowered for token in ("list", "read", "inbox")
        ) else "send_message"
    elif adapter_name == "contacts":
        capability_name = "resolve_contact"
    elif adapter_name == "phone":
        capability_name = "place_call"
    elif adapter_name == "mail":
        capability_name = "list_mail" if any(
            token in lowered for token in ("list", "read", "inbox")
        ) else "mail_send"
    elif adapter_name == "smart_home":
        capability_name = "home_status" if any(
            token in lowered for token in ("status", "list", "read")
        ) else "home_act"
    else:
        capability_name = f"{adapter_name}.{action_name}"
    scope = str(adapter_spec.scope or "")
    high_risk = scope in {"phone:act", "home:act", "drone:act", "printer:act"}
    acting = bool(
        high_risk
        or scope.endswith(":act")
        or scope.endswith(":write")
        or any(token in lowered for token in ("send", "create", "set", "write", "call"))
    )
    risk: RiskClass = "R3" if high_risk else "R2" if acting else "R0"
    policy_spec = {
        "name": capability_name,
        "description": str(adapter_spec.description or action_name),
        "parameters": dict(adapter_spec.parameters or {"type": "object"}),
        "output": {"type": "object"},
        "permission": scope,
        "required_scopes": [scope] if scope else [],
        "risk_class": risk,
        "confirmation": confirmation_policy_for(risk),
        "target_ownership": "owner",
        "provider": adapter_name,
        "read_only": not acting,
        "sensitive": acting,
        "evidence": ["source", "timestamp"],
    }
    if adapter_name == "device_proxy" and scope == "phone:act":
        policy_spec["queue_only"] = True
    return capability_name, policy_spec


async def _authorize_integration_binding(
    session: AsyncSession,
    *,
    integration: Integration,
    adapter,
    action: str,
    adapter_spec,
    arguments: dict,
    actor: str,
    device_id=None,
    confirmation=None,
) -> None:
    from app.ev.policy import authorize

    capability_name, policy_spec = _integration_policy_spec(
        adapter.slug,
        action,
        adapter_spec,
    )
    decision = await authorize(
        session,
        capability_name,
        actor=actor,
        arguments=arguments,
        device_id=device_id,
        channel="action",
        confirmation=confirmation,
        spec=policy_spec,
        provider_scopes_override=list(integration.scopes or []),
        provider_connected_override=True,
    )
    if not decision.allowed:
        raise PermissionError(decision.reason)


async def authorize_integration_action(
    session: AsyncSession,
    integration_id: UUID,
    *,
    action: str,
    args: dict,
    actor: str,
    device_id=None,
    confirmation=None,
) -> None:
    """Policy-only preflight for HTTP/device callers before adapter dispatch."""

    integration = await _active_integration(session, integration_id)
    adapter = registry.get(integration.adapter)
    if adapter is None:
        raise LookupError(f"adapter '{integration.adapter}' is unavailable")
    adapter_spec = adapter.action(action)
    if adapter_spec is None:
        raise ValueError(f"adapter '{adapter.slug}' has no action '{action}'")
    if adapter_spec.scope not in (integration.scopes or []):
        raise PermissionError(f"scope '{adapter_spec.scope}' is not granted")
    effective_args, issues = validate_arguments(args or {}, adapter_spec.parameters)
    if issues:
        raise ValueError("; ".join(issues))
    await _authorize_integration_binding(
        session,
        integration=integration,
        adapter=adapter,
        action=action,
        adapter_spec=adapter_spec,
        arguments=effective_args,
        actor=actor,
        device_id=device_id,
        confirmation=confirmation,
    )


async def execute_action(
    session: AsyncSession,
    integration_id: UUID,
    action: str,
    args: dict,
    actor: str,
    *,
    device_id=None,
    policy_checked: bool = False,
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
    effective_args, issues = validate_arguments(args or {}, spec.parameters)
    if issues:
        raise ValueError("; ".join(issues))
    if not policy_checked:
        await _authorize_integration_binding(
            session,
            integration=integration,
            adapter=adapter,
            action=action,
            adapter_spec=spec,
            arguments=effective_args,
            actor=actor,
            device_id=device_id,
        )
    config = dict(integration.config or {})
    config["_session"] = session
    provider = config.get("provider")

    if provider == "device_proxy" and adapter.slug == "device_proxy":
        return await _queue_device_action(
            session,
            integration=integration,
            adapter=adapter,
            action=action,
            args=effective_args,
            actor=actor,
        )

    # Life adapters (macos_life / device_proxy) are authenticated by the local
    # helper + TCC, not by a vaulted OAuth token. Local doubles are CI-only and
    # must never require a provider credential.
    needs_oauth = provider not in ("macos_life", "device_proxy", "local")
    token = ""
    credential = await _credential(session, integration_id, "oauth")
    if needs_oauth:
        if (
            credential is None
            or credential.revoked_at is not None
            or not credential.encrypted_access
        ):
            raise LookupError("integration is not authorized with OAuth credentials")
        expires_at = _as_aware(credential.expires_at)
        if expires_at is not None and expires_at <= utcnow():
            if not credential.encrypted_refresh:
                raise oauth.OAuthReauthRequiredError(
                    "integration credential has expired and has no refresh token; "
                    "re-authorize to continue"
                )
            await refresh_oauth(session, integration_id, actor)
            credential = await _credential(session, integration_id, "oauth")
            if credential is None or not credential.encrypted_access:
                raise oauth.OAuthReauthRequiredError(
                    "integration requires re-authorization after a failed refresh"
                ) from None
        if credential is None or not credential.encrypted_access:
            raise LookupError("integration is not authorized with OAuth credentials")
        token = vault.decrypt(credential.encrypted_access)
    try:
        try:
            result = await adapter.act(
                action=action,
                args=effective_args,
                token=token,
                scopes=list(integration.scopes or []),
                config=config,
            )
        except oauth.OAuthAuthError:
            if not needs_oauth:
                raise LifeHelperError(
                    "life provider rejected the request",
                    error_code="life_provider_auth",
                ) from None
            if not credential or not credential.encrypted_refresh:
                raise oauth.OAuthReauthRequiredError(
                    "integration credential was rejected and has no refresh "
                    "token; re-authorize to continue"
                ) from None
            await refresh_oauth(session, integration_id, actor)
            credential = await _credential(session, integration_id, "oauth")
            if credential is None or not credential.encrypted_access:
                raise oauth.OAuthReauthRequiredError(
                    "integration requires re-authorization after a failed refresh"
                ) from None
            token = vault.decrypt(credential.encrypted_access)
            result = await adapter.act(
                action=action,
                args=effective_args,
                token=token,
                scopes=list(integration.scopes or []),
                config=config,
            )
    finally:
        if token:
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
            "provider": provider or "local",
        },
    )
    return IntegrationActionOut(
        adapter=adapter.slug,
        action=action,
        result=result,
        executed_at=utcnow(),
    )


async def execute_action_after_policy(
    session: AsyncSession,
    integration_id: UUID,
    action: str,
    args: dict,
    *,
    actor: str,
    device_id=None,
) -> IntegrationActionOut:
    """Dispatch after an outer policy preflight without double confirmation.

    A few local callers and test doubles still expose the historical
    ``execute_action`` signature. The compatibility retry is limited to the
    missing keyword error; adapter/runtime TypeErrors are never swallowed.
    """

    try:
        if device_id is not None:
            return await execute_action(
                session,
                integration_id,
                action,
                args,
                actor,
                device_id=device_id,
                policy_checked=True,
            )
        return await execute_action(
            session,
            integration_id,
            action,
            args,
            actor,
            policy_checked=True,
        )
    except TypeError as exc:
        if "unexpected keyword argument 'policy_checked'" not in str(exc):
            raise
        if device_id is not None:
            return await execute_action(
                session,
                integration_id,
                action,
                args,
                actor,
                device_id=device_id,
            )
        return await execute_action(session, integration_id, action, args, actor)


async def _queue_device_action(
    session: AsyncSession,
    *,
    integration: Integration,
    adapter: object,
    action: str,
    args: dict,
    actor: str,
) -> IntegrationActionOut:
    """Persist one device_proxy outbound action; delivery stays unconfirmed."""
    if not hasattr(adapter, "queue_payload"):
        raise LifeHelperError(
            "device_proxy adapter cannot queue this action",
            error_code="device_proxy_invalid_action",
        )
    target_device = args.get("device_id")
    device_id: UUID | None = None
    if isinstance(target_device, str) and target_device:
        try:
            device_id = UUID(target_device)
        except ValueError as exc:
            raise ValueError("device_id must be a valid UUID") from exc
    payload = await adapter.queue_payload(  # type: ignore[attr-defined]
        action=action,
        args=args,
        scopes=list(integration.scopes or []),
        config=integration.config or {},
        device_id=str(device_id) if device_id else None,
    )
    row = LifeOutboundAction(
        integration_id=integration.id,
        device_id=device_id,
        action=action,
        args=payload.get("args") or {},
        status="queued",
    )
    session.add(row)
    await session.flush()
    await log_access(
        session,
        actor=actor,
        action="integration.life_queue",
        endpoint="POST /v1/integrations/{id}/actions",
        resource_type="life_outbound_action",
        resource_ids=[row.id],
        details={
            "adapter": integration.adapter,
            "action": action,
            "mode": "device_proxy",
            "queued": True,
        },
    )
    return IntegrationActionOut(
        adapter=integration.adapter,
        action=action,
        result={
            "ok": True,
            "mode": "device_proxy",
            "action": action,
            "queued": True,
            "queue_id": str(row.id),
            "delivery": {"confirmed": False, "status": "queued"},
            "policy": payload.get("policy"),
        },
        executed_at=utcnow(),
    )


async def life_policy_decision(
    session: AsyncSession,
    integration_id: UUID,
    *,
    action: str,
    recipient: str | None,
    confirm: bool = False,
) -> LifePolicyOut:
    integration = await _active_integration(session, integration_id)
    adapter = registry.get(integration.adapter)
    if adapter is None:
        raise LookupError(f"adapter '{integration.adapter}' is unavailable")
    if adapter.action(action) is None:
        raise ValueError(f"adapter '{adapter.slug}' has no action '{action}'")
    decision = evaluate_life_policy(
        scopes=list(integration.scopes or []),
        action=action,
        recipient=recipient,
        confirm=confirm,
        allowlist=(integration.config or {}).get("contact_allowlist"),
        autonomy=(integration.config or {}).get("autonomy"),
        confirm_unknown=(integration.config or {}).get("confirm_unknown"),
    )
    return LifePolicyOut(**decision.to_dict())


async def ingest_device_result(
    session: AsyncSession,
    integration_id: UUID,
    data: LifeDeviceResultIn,
    *,
    actor: str,
    device_id: UUID | None,
) -> LifeDeviceResultOut:
    """Accept an authenticated device-posted delivery result (never fake)."""
    integration = await _active_integration(session, integration_id)
    if (integration.config or {}).get("provider") != "device_proxy":
        raise ValueError("integration is not a device_proxy adapter")
    queue_id = data.queue_id
    if queue_id is not None:
        row = await session.get(LifeOutboundAction, queue_id)
        if row is None or row.integration_id != integration.id:
            raise KeyError("life outbound action not found")
        if row.status != "queued":
            raise ValueError(f"life outbound action is already {row.status}")
        if row.device_id is not None and device_id is not None and row.device_id != device_id:
            raise PermissionError("life outbound action is assigned to another device")
        if data.status == "delivered":
            evidence = data.evidence or {}
            has_evidence = bool(
                evidence.get("message_id") or evidence.get("call_id")
            ) and bool(evidence.get("sent_at") or evidence.get("dialed_at") or evidence.get("completed_at"))
            if not has_evidence:
                raise LifeHelperError(
                    "device reported delivered without delivery evidence; "
                    "refusing to mark delivered",
                    error_code="missing_delivery_evidence",
                )
            row.status = "delivered"
            row.evidence = evidence
            row.delivered_at = utcnow()
        else:
            row.status = "failed"
            row.error = data.error or "device reported failure"
            row.result = {"device_id": str(device_id) if device_id else None}
        row.updated_at = utcnow()
        await session.flush()
        await log_access(
            session,
            actor=actor,
            action="integration.life_device_result",
            endpoint="POST /v1/integrations/{id}/life/device-results",
            resource_type="life_outbound_action",
            resource_ids=[row.id],
            details={
                "status": row.status,
                "evidence_keys": sorted((row.evidence or {}).keys()),
            },
        )
        return LifeDeviceResultOut(
            accepted=True,
            queue_id=row.id,
            status=row.status,
            delivery={"confirmed": row.status == "delivered"},
        )
    if data.message:
        channel = await session.get(LiveChannel, integration.live_channel_id)
        if channel is None or not channel.active:
            raise LookupError("integration live channel is inactive")
        stored = await ingest_events(
            session,
            channel,
            [
                _device_message_event(data.message, device_id)
            ],
        )
        await log_access(
            session,
            actor=actor,
            action="integration.life_device_result",
            endpoint="POST /v1/integrations/{id}/life/device-results",
            resource_type="live_event",
            resource_ids=[event.id for event in stored],
            details={"kind": "message"},
        )
        return LifeDeviceResultOut(
            accepted=bool(stored),
            status="recorded",
            delivery={"confirmed": True, "evidence": "live_event"},
        )
    raise ValueError("device result requires queue_id or message")


def _device_message_event(message: dict, device_id: UUID | None) -> LiveEventCreate:
    return LiveEventCreate(
        event_type="message.received",
        payload={
            "sender": _short_text(message.get("sender"), 128) or "unknown",
            "channel": _short_text(message.get("channel"), 128) or "sms",
            "text": _short_text(message.get("text"), 2000),
            "device_id": str(device_id) if device_id else None,
            "source": "device_proxy",
        },
        device_id=str(device_id) if device_id else None,
    )


async def list_device_outbox(
    session: AsyncSession,
    integration_id: UUID,
    *,
    device_id: UUID | None,
    limit: int = 100,
) -> LifeOutboxOut:
    integration = await _active_integration(session, integration_id)
    if (integration.config or {}).get("provider") != "device_proxy":
        raise ValueError("integration is not a device_proxy adapter")
    stmt = (
        select(LifeOutboundAction)
        .where(
            LifeOutboundAction.integration_id == integration.id,
            LifeOutboundAction.status == "queued",
        )
        .order_by(LifeOutboundAction.created_at.asc())
        .limit(min(limit, 500))
    )
    if device_id is not None:
        stmt = stmt.where(
            (LifeOutboundAction.device_id.is_(None))
            | (LifeOutboundAction.device_id == device_id)
        )
    rows = list((await session.execute(stmt)).scalars().all())
    return LifeOutboxOut(
        items=[
            LifeOutboxEntryOut(
                id=row.id,
                action=row.action,
                args=row.args or {},
                created_at=row.created_at,
            )
            for row in rows
        ]
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
    if adapter.slug == "smart_home":
        from app.ev.home import apply_observed_updates

        await apply_observed_updates(session, events)
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
