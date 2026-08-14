"""Integrations & ecosystem API: adapters, vault, webhooks, plugins."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    ActorContext,
    require_actor,
    require_actor_context,
    require_master,
    require_reverification,
)
from app.config import settings
from app.db import get_session
from app.integrations import oauth, webhooks
from app.integrations import plugins as plugin_service
from app.integrations import service as integrations
from app.integrations.life_helper import (
    LifeHelperError,
    LifeHelperUnavailableError,
    LifePermissionDeniedError,
)
from app.models import Integration
from app.schemas import (
    IntegrationActionOut,
    IntegrationActionRequest,
    IntegrationCatalogItem,
    IntegrationCreate,
    IntegrationCredentialCreate,
    IntegrationCredentialOut,
    IntegrationOut,
    IntegrationScopeUpdate,
    IntegrationSyncOut,
    LifeDeviceResultIn,
    LifeDeviceResultOut,
    LifeOutboxOut,
    LifePolicyOut,
    LiveEventOut,
    OAuthAuthorizeOut,
    OAuthStatusOut,
    PluginCommandOut,
    PluginCommandRequest,
    PluginManifest,
    PluginOut,
    PluginRejectRequest,
    VaultRotateOut,
    VaultRotateRequest,
    WebhookIngestOut,
    WebhookSecretOut,
)

router = APIRouter(prefix="/v1")


def _integration_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail="Integration not found")
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, LookupError):
        return HTTPException(status_code=410, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, oauth.OAuthReauthRequiredError):
        return HTTPException(status_code=401, detail=str(exc))
    if isinstance(exc, oauth.OAuthAuthError):
        return HTTPException(status_code=401, detail=str(exc))
    if isinstance(exc, oauth.OAuthProviderError):
        return HTTPException(status_code=502, detail=str(exc))
    if isinstance(exc, LifePermissionDeniedError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (LifeHelperError, LifeHelperUnavailableError)):
        return HTTPException(status_code=502, detail=str(exc))
    raise exc


def _plugin_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    raise exc


# --------------------------------------------------------------------------- #
# Adapter catalog & integration lifecycle
# --------------------------------------------------------------------------- #


@router.get("/integrations/catalog", response_model=list[IntegrationCatalogItem])
async def catalog(actor: str = Depends(require_actor)) -> list[IntegrationCatalogItem]:
    return integrations.catalog()


@router.get("/integrations", response_model=list[IntegrationOut])
async def list_integrations(
    include_revoked: bool = False,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[IntegrationOut]:
    return await integrations.list_integrations(session, include_revoked=include_revoked)


@router.post("/integrations", response_model=IntegrationOut, status_code=201)
async def install_integration(
    data: IntegrationCreate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_master),
) -> IntegrationOut:
    try:
        row = await integrations.install(session, data, actor=actor)
    except Exception as exc:
        raise _integration_error(exc) from exc
    await session.commit()
    return IntegrationOut.model_validate(row)


@router.get("/integrations/{integration_id}", response_model=IntegrationOut)
async def get_integration(
    integration_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> IntegrationOut:
    try:
        return await integrations.get_integration(session, integration_id)
    except Exception as exc:
        raise _integration_error(exc) from exc


@router.patch("/integrations/{integration_id}/scopes", response_model=IntegrationOut)
async def update_integration_scopes(
    integration_id: UUID,
    data: IntegrationScopeUpdate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_master),
) -> IntegrationOut:
    try:
        result = await integrations.update_scopes(session, integration_id, data, actor=actor)
    except Exception as exc:
        raise _integration_error(exc) from exc
    await session.commit()
    return result


@router.delete("/integrations/{integration_id}", response_model=IntegrationOut)
async def revoke_integration(
    integration_id: UUID,
    reason: str = "user revoked",
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_master),
) -> IntegrationOut:
    try:
        result = await integrations.revoke(session, integration_id, actor=actor, reason=reason)
    except Exception as exc:
        raise _integration_error(exc) from exc
    await session.commit()
    return result


@router.post("/integrations/vault/rotate", response_model=VaultRotateOut)
async def rotate_integration_vault(
    data: VaultRotateRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_master),
) -> VaultRotateOut:
    try:
        result = await integrations.rotate_vault(
            session,
            new_key=data.new_key,
            actor=actor,
        )
    except Exception as exc:
        raise _integration_error(exc) from exc
    await session.commit()
    return VaultRotateOut(**result)


# --------------------------------------------------------------------------- #
# Credential vault & webhook secrets
# --------------------------------------------------------------------------- #


@router.post(
    "/integrations/{integration_id}/credentials",
    response_model=IntegrationCredentialOut,
    status_code=201,
)
async def store_oauth_credential(
    integration_id: UUID,
    data: IntegrationCredentialCreate,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_master),
) -> IntegrationCredentialOut:
    try:
        result = await integrations.store_oauth(session, integration_id, data, actor=actor)
    except Exception as exc:
        raise _integration_error(exc) from exc
    await session.commit()
    return result


@router.get(
    "/integrations/{integration_id}/credentials",
    response_model=list[IntegrationCredentialOut],
)
async def list_credentials(
    integration_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_master),
) -> list[IntegrationCredentialOut]:
    try:
        row = await session.get(Integration, integration_id)
        if row is None:
            raise KeyError(integration_id)
    except Exception as exc:
        raise _integration_error(exc) from exc
    return await integrations.list_credentials(session, integration_id)


@router.post(
    "/integrations/{integration_id}/credentials/refresh",
    response_model=IntegrationCredentialOut,
)
async def refresh_oauth_credential(
    integration_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_master),
) -> IntegrationCredentialOut:
    try:
        result = await integrations.refresh_oauth(session, integration_id, actor=actor)
    except Exception as exc:
        raise _integration_error(exc) from exc
    await session.commit()
    return result


@router.get(
    "/integrations/oauth/authorize",
    response_model=OAuthAuthorizeOut,
)
async def oauth_authorize(
    integration_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_master),
) -> OAuthAuthorizeOut:
    """Start an authorization-code + PKCE flow for a provider-backed adapter."""
    try:
        result = await integrations.begin_oauth_flow(session, integration_id, actor=actor)
    except Exception as exc:
        raise _integration_error(exc) from exc
    await session.commit()
    return result


@router.get(
    "/integrations/oauth/callback",
    response_model=IntegrationCredentialOut,
)
async def oauth_callback(
    code: str,
    state: str,
    session: AsyncSession = Depends(get_session),
) -> IntegrationCredentialOut:
    """Provider redirect target: exchange the code, store tokens in the vault.

    The CSRF ``state`` value is the only credential; the callback is public so
    provider browsers can redirect here without a bearer token.
    """
    try:
        result = await integrations.complete_oauth_flow(
            session,
            state=state,
            code=code,
            actor="oauth_callback",
        )
    except Exception as exc:
        raise _integration_error(exc) from exc
    await session.commit()
    return result


@router.post(
    "/integrations/oauth/callback",
    response_model=IntegrationCredentialOut,
)
async def oauth_callback_post(
    code: str,
    state: str,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_master),
) -> IntegrationCredentialOut:
    """Headless/CLI alternative to the browser redirect callback."""
    try:
        result = await integrations.complete_oauth_flow(
            session,
            state=state,
            code=code,
            actor=actor,
        )
    except Exception as exc:
        raise _integration_error(exc) from exc
    await session.commit()
    return result


@router.get(
    "/integrations/{integration_id}/oauth/status",
    response_model=OAuthStatusOut,
)
async def oauth_status(
    integration_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> OAuthStatusOut:
    try:
        return await integrations.oauth_status(session, integration_id)
    except Exception as exc:
        raise _integration_error(exc) from exc


@router.post(
    "/integrations/{integration_id}/webhook-secret",
    response_model=WebhookSecretOut,
    status_code=201,
)
async def rotate_webhook_secret(
    integration_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_master),
) -> WebhookSecretOut:
    try:
        result = await integrations.create_webhook_secret(session, integration_id, actor=actor)
    except Exception as exc:
        raise _integration_error(exc) from exc
    await session.commit()
    return result


# --------------------------------------------------------------------------- #
# Actions, events, webhook ingress
# --------------------------------------------------------------------------- #


@router.post(
    "/integrations/{integration_id}/actions",
    response_model=IntegrationActionOut,
)
async def run_integration_action(
    integration_id: UUID,
    data: IntegrationActionRequest,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_reverification("integration.action")),
) -> IntegrationActionOut:
    actor = ctx.actor
    try:
        result = await integrations.execute_action(
            session,
            integration_id,
            action=data.action,
            args=data.args,
            actor=actor,
        )
    except Exception as exc:
        raise _integration_error(exc) from exc
    await session.commit()
    return result


@router.post(
    "/integrations/{integration_id}/sync",
    response_model=IntegrationSyncOut,
)
async def sync_integration(
    integration_id: UUID,
    days: int | None = None,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_reverification("integration.action")),
) -> IntegrationSyncOut:
    """Pull real provider data (e.g. 7 days of calendar events) into live events."""
    actor = ctx.actor
    effective_days = min(max(days, 1), 90) if days is not None else None
    try:
        result = await integrations.sync_integration(
            session,
            integration_id,
            actor=actor,
            days=effective_days,
        )
    except Exception as exc:
        raise _integration_error(exc) from exc
    await session.commit()
    return result


@router.get(
    "/integrations/{integration_id}/calendar/signals",
    response_model=dict,
)
async def calendar_signals(
    integration_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> dict:
    """Derived calendar signals (next event, leave-by, density, quiet hours)."""
    try:
        return await integrations.calendar_signals(session, integration_id)
    except Exception as exc:
        raise _integration_error(exc) from exc


@router.get(
    "/integrations/{integration_id}/life/policy",
    response_model=LifePolicyOut,
)
async def life_policy(
    integration_id: UUID,
    action: str,
    recipient: str | None = None,
    confirm: bool = False,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> LifePolicyOut:
    """Standing-authority policy for a life action (never fabricates consent)."""
    try:
        return await integrations.life_policy_decision(
            session,
            integration_id,
            action=action,
            recipient=recipient,
            confirm=confirm,
        )
    except Exception as exc:
        raise _integration_error(exc) from exc


@router.get(
    "/integrations/{integration_id}/life/outbox",
    response_model=LifeOutboxOut,
)
async def life_outbox(
    integration_id: UUID,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> LifeOutboxOut:
    """Queued outbound actions for a registered device actuator (poll)."""
    device_id = ctx.device_id
    try:
        return await integrations.list_device_outbox(
            session,
            integration_id,
            device_id=device_id,
        )
    except Exception as exc:
        raise _integration_error(exc) from exc


@router.post(
    "/integrations/{integration_id}/life/device-results",
    response_model=LifeDeviceResultOut,
)
async def life_device_results(
    integration_id: UUID,
    data: LifeDeviceResultIn,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_actor_context),
) -> LifeDeviceResultOut:
    """Authenticated device-posted delivery results (evidence required)."""
    try:
        result = await integrations.ingest_device_result(
            session,
            integration_id,
            data,
            actor=ctx.actor,
            device_id=ctx.device_id,
        )
    except Exception as exc:
        raise _integration_error(exc) from exc
    await session.commit()
    return result


@router.get("/integrations/{integration_id}/events", response_model=list[LiveEventOut])
async def list_integration_events(
    integration_id: UUID,
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[LiveEventOut]:
    try:
        rows = await integrations.integration_events(session, integration_id, limit=min(limit, 500))
    except Exception as exc:
        raise _integration_error(exc) from exc
    return [LiveEventOut.model_validate(row) for row in rows]


@router.post("/integrations/webhook/{integration_id}", response_model=WebhookIngestOut)
async def webhook_ingest(
    integration_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> WebhookIngestOut:
    """External webhook ingress: HMAC-verified, replay-protected, rate-limited."""
    body = await request.body()
    if len(body) > settings.webhook_max_body_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"webhook body exceeds {settings.webhook_max_body_bytes} bytes",
        )
    try:
        result = await integrations.ingest_webhook(
            session,
            integration_id,
            body,
            dict(request.headers),
        )
    except webhooks.SignatureError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except webhooks.RateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except Exception as exc:
        raise _integration_error(exc) from exc
    await session.commit()
    return result


# --------------------------------------------------------------------------- #
# Plugin framework
# --------------------------------------------------------------------------- #


@router.get("/plugins", response_model=list[PluginOut])
async def list_plugins(
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> list[PluginOut]:
    return await plugin_service.list_plugins(session)


@router.post("/plugins", response_model=PluginOut, status_code=201)
async def submit_plugin(
    data: PluginManifest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_master),
) -> PluginOut:
    try:
        row = await plugin_service.submit(session, data.model_dump(), actor=actor)
    except Exception as exc:
        raise _plugin_error(exc) from exc
    await session.commit()
    return PluginOut.model_validate(row)


@router.get("/plugins/{plugin_id}", response_model=PluginOut)
async def get_plugin(
    plugin_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
) -> PluginOut:
    try:
        return await plugin_service.get_plugin(session, plugin_id)
    except Exception as exc:
        raise _plugin_error(exc) from exc


@router.post("/plugins/{plugin_id}/approve", response_model=PluginOut)
async def approve_plugin(
    plugin_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_master),
) -> PluginOut:
    try:
        result = await plugin_service.approve(session, plugin_id, actor=actor)
    except Exception as exc:
        raise _plugin_error(exc) from exc
    await session.commit()
    return result


@router.post("/plugins/{plugin_id}/reject", response_model=PluginOut)
async def reject_plugin(
    plugin_id: UUID,
    data: PluginRejectRequest,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_master),
) -> PluginOut:
    try:
        result = await plugin_service.reject(session, plugin_id, actor=actor, reason=data.reason)
    except Exception as exc:
        raise _plugin_error(exc) from exc
    await session.commit()
    return result


@router.post("/plugins/{plugin_id}/disable", response_model=PluginOut)
async def disable_plugin(
    plugin_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_master),
) -> PluginOut:
    try:
        result = await plugin_service.set_enabled(session, plugin_id, actor=actor, enabled=False)
    except Exception as exc:
        raise _plugin_error(exc) from exc
    await session.commit()
    return result


@router.post("/plugins/{plugin_id}/enable", response_model=PluginOut)
async def enable_plugin(
    plugin_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_master),
) -> PluginOut:
    try:
        result = await plugin_service.set_enabled(session, plugin_id, actor=actor, enabled=True)
    except Exception as exc:
        raise _plugin_error(exc) from exc
    await session.commit()
    return result


@router.post(
    "/plugins/{plugin_id}/commands/{command_name}",
    response_model=PluginCommandOut,
)
async def run_plugin_command(
    plugin_id: UUID,
    command_name: str,
    data: PluginCommandRequest,
    session: AsyncSession = Depends(get_session),
    ctx: ActorContext = Depends(require_reverification("integration.action")),
) -> PluginCommandOut:
    actor = ctx.actor
    try:
        result = await plugin_service.run_command(
            session,
            plugin_id,
            command_name,
            args=data.args,
            actor=actor,
        )
    except Exception as exc:
        raise _plugin_error(exc) from exc
    await session.commit()
    return result
