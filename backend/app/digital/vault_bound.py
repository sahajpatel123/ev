"""Credential boundary: adapters may hold tokens; Muse never sees them.

Tokens live in ``app.integrations.vault``. This module fetches/refreshes them
for a Digital Operations call and drops plaintext as soon as the HTTP call
returns. Return values, logs, drafts, and model context must not include
access tokens, refresh tokens, cookies, or Authorization headers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations import oauth, vault
from app.models import Integration, IntegrationCredential

logger = logging.getLogger("ev.digital.vault")

_SECRET_KEYS = frozenset(
    {
        "access_token",
        "refresh_token",
        "id_token",
        "token",
        "authorization",
        "cookie",
        "cookies",
        "set-cookie",
        "password",
        "secret",
        "api_key",
        "apikey",
        "client_secret",
        "session",
    }
)


@dataclass(frozen=True)
class TokenLease:
    """In-memory lease. Do not serialize. Do not log."""

    integration_id: UUID
    adapter: str
    scopes: tuple[str, ...]
    account: str | None
    _access: str

    def header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access}"}

    def has_scope(self, *needed: str) -> bool:
        have = set(self.scopes)
        return all(s in have or s.split("/")[-1] in have for s in needed)


def strip_secrets(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            key = str(k).lower().replace("-", "_")
            if key in _SECRET_KEYS or "cookie" in key:
                continue
            out[k] = strip_secrets(v)
        return out
    if isinstance(obj, list):
        return [strip_secrets(v) for v in obj]
    return obj


_ADAPTER_FALLBACKS: dict[str, tuple[str, ...]] = {
    "mail": ("mail", "gmail_ops"),
    "gmail_ops": ("gmail_ops", "mail"),
    "contacts": ("contacts", "mail", "gmail_ops"),
    "calendar": ("calendar",),
}


async def lease_for(
    session: AsyncSession,
    adapter: str,
    *,
    actor: str = "digital-ops",
) -> TokenLease | None:
    """Return a usable access-token lease, refreshing if it expires within 90s.

    Skip active integrations that have no vaulted OAuth row (e.g. Mail.app
    helper) so they cannot shadow a later Google grant on the same adapter.
    """
    slugs = _ADAPTER_FALLBACKS.get(adapter, (adapter,))
    row = None
    cred = None
    for slug in slugs:
        rows = (
            await session.execute(
                select(Integration)
                .where(Integration.adapter == slug, Integration.status == "active")
                .order_by(Integration.created_at.asc())
            )
        ).scalars().all()
        for candidate in rows:
            found = (
                await session.execute(
                    select(IntegrationCredential).where(
                        IntegrationCredential.integration_id == candidate.id,
                        IntegrationCredential.kind == "oauth",
                        IntegrationCredential.revoked_at.is_(None),
                    )
                )
            ).scalars().first()
            if found is not None and found.encrypted_access:
                row = candidate
                cred = found
                break
        if row is not None:
            break
    if row is None or cred is None or not cred.encrypted_access:
        return None
    access = vault.decrypt(cred.encrypted_access)
    expires = cred.expires_at
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    soon = datetime.now(UTC) + timedelta(seconds=90)
    if cred.encrypted_refresh and expires is not None and expires <= soon:
        try:
            from app.integrations.service import refresh_oauth

            await refresh_oauth(session, row.id, actor)
            await session.flush()
            cred = (
                await session.execute(
                    select(IntegrationCredential).where(
                        IntegrationCredential.id == cred.id
                    )
                )
            ).scalars().first()
            if cred is None or not cred.encrypted_access:
                return None
            access = vault.decrypt(cred.encrypted_access)
        except Exception:
            logger.info("oauth refresh failed for adapter=%s; using current token", adapter)
    scopes = tuple(str(s) for s in (cred.scopes or row.scopes or []))
    return TokenLease(
        integration_id=row.id,
        adapter=adapter,
        scopes=scopes,
        account=cred.provider_account_id,
        _access=access,
    )


def diagnose_oauth_error(exc: BaseException) -> str:
    if isinstance(exc, oauth.OAuthReauthRequiredError):
        return "oauth_expired"
    if isinstance(exc, oauth.OAuthAuthError):
        msg = str(exc).lower()
        if "insufficient" in msg or "scope" in msg:
            return "scope_missing"
        if "accessnotconfigured" in msg or "has not been used" in msg or "api_not_enabled" in msg:
            return "api_not_enabled"
        return "oauth_rejected"
    text = str(exc).lower()
    if "429" in text or "rate" in text:
        return "rate_limited"
    if "404" in text or "not found" in text:
        return "message_missing"
    if "network" in text or "connect" in text:
        return "api_unavailable"
    return "api_unavailable"
