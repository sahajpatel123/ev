"""Provider-specific OAuth 2.0 flows: authorization-code + PKCE, refresh, revoke.

This module owns the *protocol* only. Credential storage stays in the vault
(see :mod:`app.integrations.vault` and the integration service); tokens never
appear in logs, prompts, errors, or webhook echoes. Provider error handling is
deliberately conservative: only status codes and short ``error`` /
``error_description`` fields are surfaced, never raw response bodies.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import settings
from app.utils.text import utcnow

MAX_ERROR_DESCRIPTION = 160
MIN_ACCESS_TOKEN_LENGTH = 8


def make_http_client(timeout: float | None = None) -> httpx.AsyncClient:
    """Shared provider HTTP client; a seam for tests to inject a mock provider."""
    return httpx.AsyncClient(timeout=timeout or settings.oauth_http_timeout_seconds)


class OAuthProviderError(Exception):
    """Provider protocol failure (message is sanitized; never contains tokens)."""


class OAuthAuthError(OAuthProviderError):
    """Provider rejected the credential (HTTP 401/403). Safe to auto-refresh."""


class OAuthReauthRequiredError(OAuthProviderError):
    """Refresh failed or the grant is dead; the human must authorize again."""


def new_pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for S256 PKCE."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _parse_expires_in(value: Any) -> datetime | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return utcnow() + timedelta(seconds=float(value))
    except (TypeError, ValueError, OverflowError):
        return None


def _provider_error(response: httpx.Response, fallback: str) -> OAuthProviderError:
    """Build a sanitized provider error: no body, no tokens, no URLs with secrets."""
    error_code = ""
    description = ""
    try:
        data = response.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        error_code = str(data.get("error") or "")[:80]
        raw_description = data.get("error_description") or data.get("message") or ""
        if isinstance(raw_description, str):
            description = raw_description[:MAX_ERROR_DESCRIPTION]
    if response.status_code in (400, 401, 403) or error_code in {
        "invalid_grant",
        "invalid_client",
        "unauthorized_client",
        "invalid_request",
        "expired_token",
    }:
        detail = f" ({error_code}: {description})" if error_code or description else ""
        if error_code in {"invalid_grant", "expired_token", "invalid_client", "unauthorized_client"}:
            return OAuthReauthRequiredError(
                f"provider authorization is no longer valid{detail}; re-authorize"
            )
        return OAuthAuthError(f"provider rejected the credential{detail}")
    return OAuthProviderError(f"{fallback} (provider status {response.status_code})")


@dataclass(frozen=True)
class OAuthProvider:
    """One authorization-code provider with PKCE + refresh + optional revoke."""

    slug: str
    name: str
    authorize_url: str
    token_url: str
    api_base: str
    scopes: tuple[str, ...]
    client_id: str
    client_secret: str
    redirect_uri: str
    extra_authorize_params: dict[str, str] = field(default_factory=dict)
    revoke_url: str | None = None

    def missing_credentials(self) -> list[str]:
        missing: list[str] = []
        if not self.client_id:
            missing.append("client id")
        if not self.client_secret:
            missing.append("client secret")
        if not self.redirect_uri:
            missing.append("redirect URI")
        return missing

    def build_authorize_url(self, *, state: str, code_verifier: str) -> str:
        code_challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        params: dict[str, str] = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.scopes),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            **self.extra_authorize_params,
        }
        return f"{self.authorize_url}?{urlencode(params)}"

    async def exchange_code(self, code: str, code_verifier: str) -> dict:
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": code_verifier,
        }
        headers: dict[str, str] = {}
        if self.slug == "github":
            headers["Accept"] = "application/json"
        async with make_http_client() as client:
            response = await client.post(self.token_url, data=data, headers=headers)
        if response.status_code >= 400:
            raise _provider_error(response, "authorization-code exchange failed")
        return self._parse_token_response(response)

    async def refresh(self, refresh_token: str) -> dict:
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        headers: dict[str, str] = {}
        if self.slug == "github":
            headers["Accept"] = "application/json"
        async with make_http_client() as client:
            response = await client.post(self.token_url, data=data, headers=headers)
        if response.status_code >= 400:
            raise _provider_error(response, "token refresh failed")
        return self._parse_token_response(response)

    async def revoke(self, access_token: str) -> dict:
        """Best-effort provider-side revocation. Raises only OAuthProviderError."""
        if not self.revoke_url:
            return {"ok": True, "mode": "no-provider-revoke"}
        if self.slug == "google":
            async with make_http_client() as client:
                response = await client.post(
                    self.revoke_url,
                    data={"token": access_token},
                )
        elif self.slug == "github":
            async with make_http_client() as client:
                response = await client.request(
                    "DELETE",
                    self.revoke_url.format(client_id=self.client_id),
                    auth=(self.client_id, self.client_secret),
                    content=json.dumps({"access_token": access_token}),
                    headers={"Content-Type": "application/json"},
                )
        else:  # pragma: no cover - only concrete providers are registered
            raise OAuthProviderError(f"no revoke implementation for '{self.slug}'")
        if response.status_code >= 400:
            raise _provider_error(response, "provider revocation failed")
        return {"ok": True, "mode": f"{self.slug}-provider"}

    def _parse_token_response(self, response: httpx.Response) -> dict:
        try:
            data = response.json()
        except ValueError as exc:
            raise OAuthProviderError("provider returned a non-JSON token response") from exc
        if not isinstance(data, dict):
            raise OAuthProviderError("provider returned a malformed token response")
        access_token = data.get("access_token")
        if not isinstance(access_token, str) or len(access_token) < MIN_ACCESS_TOKEN_LENGTH:
            raise OAuthProviderError("provider token response is missing a valid access_token")
        refresh_token = data.get("refresh_token")
        if refresh_token is not None and (
            not isinstance(refresh_token, str) or len(refresh_token) < MIN_ACCESS_TOKEN_LENGTH
        ):
            refresh_token = None
        token_type = data.get("token_type") or "Bearer"
        if not isinstance(token_type, str):
            token_type = "Bearer"
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": token_type,
            "expires_at": _parse_expires_in(data.get("expires_in")),
            "id_token": data.get("id_token") if isinstance(data.get("id_token"), str) else None,
        }


def _google_provider() -> OAuthProvider:
    return OAuthProvider(
        slug="google",
        name="Google Calendar",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        api_base="https://www.googleapis.com/calendar/v3",
        scopes=(
            "openid",
            "email",
            "https://www.googleapis.com/auth/calendar.readonly",
        ),
        client_id=settings.google_oauth_client_id or "",
        client_secret=settings.google_oauth_client_secret or "",
        redirect_uri=settings.google_oauth_redirect_uri or "",
        extra_authorize_params={"access_type": "offline", "prompt": "consent"},
        revoke_url="https://oauth2.googleapis.com/revoke",
    )


def _github_provider() -> OAuthProvider:
    return OAuthProvider(
        slug="github",
        name="GitHub",
        authorize_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",
        api_base="https://api.github.com",
        # "repo" is required for private-repo issues/PR reads. The human can
        # instead paste a fine-grained PAT via the manual credentials endpoint
        # (no refresh/revoke); see docs/INTEGRATIONS.md.
        scopes=("repo",),
        client_id=settings.github_oauth_client_id or "",
        client_secret=settings.github_oauth_client_secret or "",
        redirect_uri=settings.github_oauth_redirect_uri or "",
        revoke_url="https://api.github.com/applications/{client_id}/grant",
    )


def provider_for(adapter_slug: str) -> OAuthProvider | None:
    if adapter_slug == "calendar":
        return _google_provider()
    if adapter_slug == "github":
        return _github_provider()
    return None


def id_token_email(outcome: dict) -> str | None:
    """Extract the account email from a Google id_token payload (display only).

    The JWT signature is intentionally not verified here: this value is used
    only as a human-readable ``provider_account_id`` label, never for
    authentication or authorization. The id_token itself is not stored.
    """
    token = outcome.get("id_token")
    if not isinstance(token, str) or token.count(".") != 2:
        return None
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        email = claims.get("email")
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error):
        return None
    if isinstance(email, str) and email:
        return email[:256]
    return None
