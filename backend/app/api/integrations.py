"""Integrations & ecosystem API: adapters, vault, webhooks, plugins."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_actor
from app.db import get_session
from app.integrations import plugins as plugin_service
from app.integrations import service as integrations
from app.integrations import webhooks
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
    LiveEventOut,
    PluginCommandOut,
    PluginCommandRequest,
    PluginManifest,
    PluginOut,
    PluginRejectRequest,
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
    actor: str = Depends(require_actor),
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
    actor: str = Depends(require_actor),
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
    actor: str = Depends(require_actor),
) -> IntegrationOut:
    try:
        result = await integrations.revoke(session, integration_id, actor=actor, reason=reason)
    except Exception as exc:
        raise _integration_error(exc) from exc
    await session.commit()
    return result


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
    actor: str = Depends(require_actor),
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
    actor: str = Depends(require_actor),
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
    actor: str = Depends(require_actor),
) -> IntegrationCredentialOut:
    try:
        result = await integrations.refresh_oauth(session, integration_id, actor=actor)
    except Exception as exc:
        raise _integration_error(exc) from exc
    await session.commit()
    return result


@router.post(
    "/integrations/{integration_id}/webhook-secret",
    response_model=WebhookSecretOut,
    status_code=201,
)
async def rotate_webhook_secret(
    integration_id: UUID,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(require_actor),
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
    actor: str = Depends(require_actor),
) -> IntegrationActionOut:
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
    actor: str = Depends(require_actor),
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
    actor: str = Depends(require_actor),
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
    actor: str = Depends(require_actor),
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
    actor: str = Depends(require_actor),
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
    actor: str = Depends(require_actor),
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
    actor: str = Depends(require_actor),
) -> PluginCommandOut:
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
