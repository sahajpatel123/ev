"""Real OAuth calendar integration: PKCE flow, sync, refresh rotation, reauth, secrets."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.integrations import oauth, vault
from app.integrations.calendar_signals import derive_calendar_signals
from app.models import AccessLog, IntegrationCredential, LiveEvent, ModelCallLog, WebhookDelivery
from app.utils.text import sha256_hex, utcnow


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _fake_id_token(email: str = "sahaj@example.com") -> str:
    header = _b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps({"email": email, "sub": "123"}).encode())
    return f"{header}.{payload}.{_b64url(b'sig')}"


FAKE_GOOGLE: dict[str, Any] = {
    "code_challenge": None,
    "state": None,
    "access_token": "google-access-token-abc123",
    "refresh_token": "google-refresh-token-xyz789",
    "rotated_access": "google-access-token-refreshed-456",
    "rotated_refresh": "google-refresh-rotated-999",
    "refresh_fail": False,
    "revoked_token": None,
    "auth_header_seen": [],
}


def _challenge(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


google_provider = FastAPI()


@google_provider.get("/o/oauth2/v2/auth")
async def google_auth(
    client_id: str,
    redirect_uri: str,
    response_type: str,
    scope: str,
    state: str,
    code_challenge: str,
    code_challenge_method: str,
    access_type: str,
    prompt: str,
) -> RedirectResponse:
    assert response_type == "code"
    assert code_challenge_method == "S256"
    assert access_type == "offline"
    assert prompt == "consent"
    FAKE_GOOGLE["code_challenge"] = code_challenge
    FAKE_GOOGLE["state"] = state
    return RedirectResponse(f"{redirect_uri}?code=fake-auth-code&state={state}")


@google_provider.post("/token")
async def google_token(request: Request) -> JSONResponse:
    form = await request.form()
    grant = form.get("grant_type")
    if grant == "authorization_code":
        code = form.get("code")
        verifier = form.get("code_verifier")
        if (
            code != "fake-auth-code"
            or verifier is None
            or _challenge(str(verifier)) != FAKE_GOOGLE["code_challenge"]
        ):
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_grant", "error_description": "bad code or verifier"},
            )
        return JSONResponse(
            {
                "access_token": FAKE_GOOGLE["access_token"],
                "refresh_token": FAKE_GOOGLE["refresh_token"],
                "expires_in": 3600,
                "token_type": "Bearer",
                "id_token": _fake_id_token(),
            }
        )
    if grant == "refresh_token":
        if FAKE_GOOGLE["refresh_fail"]:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_grant", "error_description": "token revoked"},
            )
        return JSONResponse(
            {
                "access_token": FAKE_GOOGLE["rotated_access"],
                "refresh_token": FAKE_GOOGLE["rotated_refresh"],
                "expires_in": 3600,
                "token_type": "Bearer",
            }
        )
    return JSONResponse(status_code=400, content={"error": "unsupported_grant_type"})


@google_provider.post("/revoke")
async def google_revoke(request: Request) -> JSONResponse:
    form = await request.form()
    FAKE_GOOGLE["revoked_token"] = form.get("token")
    return JSONResponse({"ok": True})


@google_provider.get("/calendar/v3/calendars/primary/events")
async def google_events(request: Request) -> JSONResponse:
    FAKE_GOOGLE["auth_header_seen"].append(request.headers.get("Authorization"))
    if request.headers.get("Authorization") != f"Bearer {FAKE_GOOGLE['access_token']}":
        return JSONResponse(
            status_code=401,
            content={"error": "unauthorized_client", "error_description": "bad token"},
        )
    now = utcnow()
    return JSONResponse(
        {
            "items": [
                {
                    "id": "evt-1",
                    "summary": "Ship review",
                    "start": {"dateTime": (now + timedelta(hours=2)).isoformat()},
                    "end": {"dateTime": (now + timedelta(hours=3)).isoformat()},
                    "location": "HQ room 3",
                    "status": "confirmed",
                    "attendees": [
                        {
                            "email": "ada@example.com",
                            "displayName": "Ada Lovelace",
                            "responseStatus": "accepted",
                        }
                    ],
                    "hangoutLink": "https://meet.google.com/abc-defg-hij",
                },
                {
                    "id": "evt-2",
                    "summary": "Focus block",
                    "start": {"date": (now + timedelta(days=1)).date().isoformat()},
                    "end": {"date": (now + timedelta(days=1)).date().isoformat()},
                    "transparency": "transparent",
                    "status": "confirmed",
                },
                {
                    "id": "evt-3",
                    "summary": "Launch plan review",
                    "start": {
                        "dateTime": (now + timedelta(days=1, hours=3)).isoformat()
                    },
                    "end": {
                        "dateTime": (now + timedelta(days=1, hours=4)).isoformat()
                    },
                    "status": "confirmed",
                },
            ]
        }
    )


FAKE_GITHUB: dict[str, Any] = {
    "comment_body": None,
    "auth_header_seen": [],
    "revoked_token": None,
}

github_provider = FastAPI()


@pytest.fixture(autouse=True)
def reset_fake_providers() -> None:
    FAKE_GOOGLE.update(
        {
            "code_challenge": None,
            "state": None,
            "access_token": "google-access-token-abc123",
            "refresh_token": "google-refresh-token-xyz789",
            "rotated_access": "google-access-token-refreshed-456",
            "rotated_refresh": "google-refresh-rotated-999",
            "refresh_fail": False,
            "revoked_token": None,
            "auth_header_seen": [],
        }
    )
    FAKE_GITHUB.update(
        {
            "comment_body": None,
            "auth_header_seen": [],
            "revoked_token": None,
            "revoked_client_id": None,
        }
    )


@github_provider.get("/repos/owner/repo/issues")
async def github_issues(request: Request) -> JSONResponse:
    FAKE_GITHUB["auth_header_seen"].append(request.headers.get("Authorization"))
    if request.headers.get("Authorization") != "Bearer github-token-123456":
        return JSONResponse(status_code=401, content={"message": "Bad credentials"})
    return JSONResponse(
        [
            {
                "number": 1,
                "title": "Fix the conduit",
                "state": "open",
                "html_url": "https://github.com/owner/repo/issues/1",
                "created_at": "2026-08-01T10:00:00Z",
                "updated_at": "2026-08-10T10:00:00Z",
                "user": {"login": "ada"},
                "comments": 2,
            }
        ]
    )


@github_provider.post("/login/oauth/access_token")
async def github_token(request: Request) -> JSONResponse:
    form = await request.form()
    if form.get("grant_type") == "refresh_token":
        return JSONResponse(
            {
                "access_token": "github-token-refreshed-789",
                "refresh_token": "github-refresh-rotated-111",
                "expires_in": 3600,
                "token_type": "Bearer",
            }
        )
    return JSONResponse(status_code=400, content={"error": "unsupported_grant_type"})


@github_provider.delete("/applications/{client_id}/grant")
async def github_revoke(request: Request, client_id: str) -> JSONResponse:
    body = await request.json()
    FAKE_GITHUB["revoked_token"] = body.get("access_token")
    FAKE_GITHUB["revoked_client_id"] = client_id
    return JSONResponse({"ok": True})


@github_provider.post("/repos/owner/repo/issues/42/comments")
async def github_comment(request: Request) -> JSONResponse:
    body = await request.json()
    FAKE_GITHUB["comment_body"] = body.get("body")
    return JSONResponse(
        {
            "id": 7,
            "html_url": "https://github.com/owner/repo/pull/42#issuecomment-7",
            "created_at": "2026-08-11T12:00:00Z",
        }
    )


async def install(client: AsyncClient, adapter: str = "calendar", **overrides) -> dict:
    defaults = {
        "calendar": ["calendar:read"],
        "github": ["github:read"],
        "health": ["health:read"],
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


async def store_oauth(
    client: AsyncClient,
    integration_id: str,
    *,
    token: str = "super-secret-token-123456",
    refresh: str | None = "refresh-token-123456",
) -> dict:
    resp = await client.post(
        f"/v1/integrations/{integration_id}/credentials",
        json={
            "access_token": token,
            "refresh_token": refresh,
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
    return {"X-EV-Signature": f"sha256={digest}", "X-EV-Timestamp": ts}


def patch_provider_clients(monkeypatch: pytest.MonkeyPatch, app: FastAPI) -> None:
    original = httpx.AsyncClient

    def factory(**kwargs):
        return original(transport=ASGITransport(app=app), base_url="https://provider.invalid")

    monkeypatch.setattr(oauth, "make_http_client", factory)
    from app.integrations import adapters

    monkeypatch.setattr(adapters, "_make_client", factory)


async def complete_consent(authorize_url: str, provider_app: FastAPI) -> None:
    """Simulate the human completing consent at the provider (captures PKCE)."""
    async with AsyncClient(
        transport=ASGITransport(app=provider_app),
        base_url="https://provider.invalid",
    ) as provider_client:
        resp = await provider_client.get(authorize_url, follow_redirects=False)
    assert resp.status_code in (302, 307)


async def _assert_no_secret_leaks(
    db_session: AsyncSession,
    secrets: list[str],
    extra_texts: list[str] | None = None,
) -> None:
    access_rows = (await db_session.execute(select(AccessLog))).scalars().all()
    model_rows = (await db_session.execute(select(ModelCallLog))).scalars().all()
    delivery_rows = (await db_session.execute(select(WebhookDelivery))).scalars().all()
    event_rows = (await db_session.execute(select(LiveEvent))).scalars().all()
    blobs: list[str] = []
    blobs.extend(json.dumps(row.details or {}) for row in access_rows)
    blobs.extend(json.dumps(row.envelope or {}) for row in model_rows)
    blobs.extend(row.error or "" for row in model_rows)
    blobs.extend(json.dumps(row.event_ids or []) for row in delivery_rows)
    blobs.extend(json.dumps(row.payload or {}) for row in event_rows)
    blobs.extend(extra_texts or [])
    for secret in secrets:
        for blob in blobs:
            assert secret not in blob, f"secret leaked into: {blob[:200]}"


async def test_authorize_url_uses_pkce_and_callback_stores_tokens(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "google_oauth_client_id", "google-client-id.apps.googleusercontent.com")
    monkeypatch.setattr(settings, "google_oauth_client_secret", "google-client-secret-123456")
    monkeypatch.setattr(
        settings,
        "google_oauth_redirect_uri",
        "http://127.0.0.1:8765/v1/integrations/oauth/callback",
    )
    patch_provider_clients(monkeypatch, google_provider)

    integration = await install(client, "calendar", config={"provider": "google"})
    integration_id = integration["id"]

    resp = await client.get(
        "/v1/integrations/oauth/authorize",
        params={"integration_id": integration_id},
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    url = out["authorize_url"]
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    params = parse_qs(url.split("?", 1)[1])
    assert params["code_challenge"][0]
    assert params["code_challenge_method"] == ["S256"]
    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]
    assert "https://www.googleapis.com/auth/calendar.readonly" in params["scope"][0]
    assert params["state"] == [out["state"]]
    assert params["client_id"] == ["google-client-id.apps.googleusercontent.com"]
    assert params["redirect_uri"] == [
        "http://127.0.0.1:8765/v1/integrations/oauth/callback"
    ]
    await complete_consent(url, google_provider)

    state_row = (
        await db_session.execute(
            select(IntegrationCredential).where(
                IntegrationCredential.integration_id == UUID(integration_id),
                IntegrationCredential.kind == "oauth_state",
            )
        )
    ).scalar_one()
    assert state_row.encrypted_access is not None
    assert state_row.encrypted_refresh is not None
    assert vault.decrypt(state_row.encrypted_access)  # code verifier encrypted
    assert vault.decrypt(state_row.encrypted_refresh) == out["state"]

    resp = await client.get(
        "/v1/integrations/oauth/callback",
        params={"code": "fake-auth-code", "state": out["state"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["configured"] is True
    assert resp.json()["provider_account_id"] == "sahaj@example.com"

    credential = (
        await db_session.execute(
            select(IntegrationCredential).where(
                IntegrationCredential.integration_id == UUID(integration_id),
                IntegrationCredential.kind == "oauth",
            )
        )
    ).scalar_one()
    assert credential.encrypted_access is not None
    assert credential.encrypted_refresh is not None
    assert vault.decrypt(credential.encrypted_access) == "google-access-token-abc123"
    assert vault.decrypt(credential.encrypted_refresh) == "google-refresh-token-xyz789"
    assert credential.token_fingerprint == sha256_hex("google-access-token-abc123")

    # PKCE state row is consumed and never re-usable.
    state_rows = (
        await db_session.execute(
            select(IntegrationCredential).where(
                IntegrationCredential.kind == "oauth_state"
            )
        )
    ).scalars().all()
    assert state_rows == []

    status = (await client.get(f"/v1/integrations/{integration_id}/oauth/status")).json()
    assert status["authorized"] is True
    assert status["reauth_required"] is False
    assert status["provider"] == "google"


async def test_calendar_sync_signals_idempotency_and_no_secret_leaks(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "google_oauth_client_id", "google-client-id.apps.googleusercontent.com")
    monkeypatch.setattr(settings, "google_oauth_client_secret", "google-client-secret-123456")
    monkeypatch.setattr(
        settings,
        "google_oauth_redirect_uri",
        "http://127.0.0.1:8765/v1/integrations/oauth/callback",
    )
    patch_provider_clients(monkeypatch, google_provider)

    integration = await install(client, "calendar", config={"provider": "google"})
    integration_id = integration["id"]
    authorize = (
        await client.get(
            "/v1/integrations/oauth/authorize",
            params={"integration_id": integration_id},
        )
    ).json()
    await complete_consent(authorize["authorize_url"], google_provider)
    await client.get(
        "/v1/integrations/oauth/callback",
        params={"code": "fake-auth-code", "state": authorize["state"]},
    )

    resp = await client.post(f"/v1/integrations/{integration_id}/sync")
    assert resp.status_code == 200, resp.text
    sync = resp.json()
    assert sync["adapter"] == "calendar"
    assert sync["accepted"] == 3  # all provider events are stored...
    assert sync["event_count"] == 3
    assert sync["signals"]["next_event"]["summary"] == "Ship review"
    assert sync["signals"]["leave_by"]
    assert sync["signals"]["today"]["event_count"] == 1  # ...density ignores free blocks
    assert len(sync["signals"]["day_density"]) == settings.calendar_sync_days + 1
    assert settings.calendar_sync_days >= 7  # acceptance: ≥ 7 days of events
    assert sync["signals"]["day_density"][0]["date"] == utcnow().date().isoformat()
    assert sync["signals"]["quiet_hours"]["active"] in (True, False)
    assert sync["signals"]["participants"][0]["email"] == "ada@example.com"

    # Idempotent: the same provider events are not duplicated on re-sync.
    resp = await client.post(f"/v1/integrations/{integration_id}/sync")
    assert resp.status_code == 200, resp.text
    again = resp.json()
    assert again["accepted"] == 0
    assert again["deduplicated"] == 3

    # Signals are also derivable from stored live events without the provider.
    resp = await client.get(f"/v1/integrations/{integration_id}/calendar/signals")
    assert resp.status_code == 200, resp.text
    signals = resp.json()
    assert signals["next_event"]["summary"] == "Ship review"
    assert signals["day_density"][0]["event_count"] == 1
    assert signals["deadline_proximity"] > 0

    # Real (non-local) read path through the adapter action.
    resp = await client.post(
        f"/v1/integrations/{integration_id}/actions",
        json={"action": "calendar.list_upcoming", "args": {}},
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert result["mode"] == "google"
    assert len(result["events"]) == 3
    assert result["events"][0]["event_id"] == "evt-1"

    # A full sync touched every log family; none of them may contain tokens.
    await _assert_no_secret_leaks(
        db_session,
        [
            "google-access-token-abc123",
            "google-refresh-token-xyz789",
            "google-client-secret-123456",
        ],
        extra_texts=[resp.text],
    )

    # Vault ciphertext never contains plaintext tokens.
    credential = (
        await db_session.execute(
            select(IntegrationCredential).where(
                IntegrationCredential.integration_id == UUID(integration_id),
                IntegrationCredential.kind == "oauth",
            )
        )
    ).scalar_one()
    assert "google-access-token-abc123" not in (credential.encrypted_access or "")
    assert "google-refresh-token-xyz789" not in (credential.encrypted_refresh or "")


async def test_refresh_token_rotation_and_clean_reauth_prompt(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "google_oauth_client_id", "google-client-id.apps.googleusercontent.com")
    monkeypatch.setattr(settings, "google_oauth_client_secret", "google-client-secret-123456")
    monkeypatch.setattr(
        settings,
        "google_oauth_redirect_uri",
        "http://127.0.0.1:8765/v1/integrations/oauth/callback",
    )
    patch_provider_clients(monkeypatch, google_provider)

    integration = await install(client, "calendar", config={"provider": "google"})
    integration_id = integration["id"]
    authorize = (
        await client.get(
            "/v1/integrations/oauth/authorize",
            params={"integration_id": integration_id},
        )
    ).json()
    await complete_consent(authorize["authorize_url"], google_provider)
    await client.get(
        "/v1/integrations/oauth/callback",
        params={"code": "fake-auth-code", "state": authorize["state"]},
    )

    credential = (
        await db_session.execute(
            select(IntegrationCredential).where(
                IntegrationCredential.integration_id == UUID(integration_id),
                IntegrationCredential.kind == "oauth",
            )
        )
    ).scalar_one()
    credential.expires_at = utcnow() - timedelta(minutes=1)
    await db_session.commit()

    # Expired token: the provider has rotated/revoked the old access token, so
    # the sync 401s, auto-refreshes, rotates both vault tokens, then succeeds.
    FAKE_GOOGLE["access_token"] = "google-access-token-refreshed-456"
    resp = await client.post(f"/v1/integrations/{integration_id}/sync")
    assert resp.status_code == 200, resp.text
    assert resp.json()["accepted"] == 3
    db_session.expire_all()
    credential = (
        await db_session.execute(
            select(IntegrationCredential).where(
                IntegrationCredential.integration_id == UUID(integration_id),
                IntegrationCredential.kind == "oauth",
            )
        )
    ).scalar_one()
    assert credential.encrypted_access is not None
    assert credential.encrypted_refresh is not None
    assert vault.decrypt(credential.encrypted_access) == "google-access-token-refreshed-456"
    assert vault.decrypt(credential.encrypted_refresh) == "google-refresh-rotated-999"
    assert credential.token_fingerprint == sha256_hex("google-access-token-refreshed-456")

    # Now the access token is rejected again and the refresh token is dead:
    # a clean re-auth prompt, no secrets.
    FAKE_GOOGLE["access_token"] = "google-access-token-rotated-again-789"
    FAKE_GOOGLE["refresh_fail"] = True
    credential.expires_at = utcnow() - timedelta(minutes=1)
    await db_session.commit()
    resp = await client.post(f"/v1/integrations/{integration_id}/sync")
    assert resp.status_code == 401, resp.text
    assert "re-authorize" in resp.json()["detail"]
    for secret in (
        "google-access-token-refreshed-456",
        "google-refresh-rotated-999",
        "google-client-secret-123456",
    ):
        assert secret not in resp.text

    status = (await client.get(f"/v1/integrations/{integration_id}/oauth/status")).json()
    assert status["reauth_required"] is True
    assert status["authorized"] is False
    await _assert_no_secret_leaks(
        db_session,
        ["google-access-token-refreshed-456", "google-refresh-rotated-999"],
        extra_texts=[resp.text],
    )


async def test_revoke_calls_google_revoke_and_wipes_vault(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "google_oauth_client_id", "google-client-id.apps.googleusercontent.com")
    monkeypatch.setattr(settings, "google_oauth_client_secret", "google-client-secret-123456")
    monkeypatch.setattr(
        settings,
        "google_oauth_redirect_uri",
        "http://127.0.0.1:8765/v1/integrations/oauth/callback",
    )
    patch_provider_clients(monkeypatch, google_provider)

    integration = await install(
        client,
        "calendar",
        config={"provider": "google", "revoke_remote": True},
    )
    integration_id = integration["id"]
    authorize = (
        await client.get(
            "/v1/integrations/oauth/authorize",
            params={"integration_id": integration_id},
        )
    ).json()
    await complete_consent(authorize["authorize_url"], google_provider)
    await client.get(
        "/v1/integrations/oauth/callback",
        params={"code": "fake-auth-code", "state": authorize["state"]},
    )

    resp = await client.delete(f"/v1/integrations/{integration_id}")
    assert resp.status_code == 200, resp.text
    assert FAKE_GOOGLE["revoked_token"] == "google-access-token-abc123"
    status = (await client.get(f"/v1/integrations/{integration_id}/oauth/status")).json()
    assert status["reauth_required"] is True
    assert status["authorized"] is False
    credential = (
        await db_session.execute(
            select(IntegrationCredential).where(
                IntegrationCredential.integration_id == UUID(integration_id),
                IntegrationCredential.kind == "oauth",
            )
        )
    ).scalar_one()
    assert credential.encrypted_access is None
    assert credential.encrypted_refresh is None
    assert credential.token_fingerprint is None


async def test_github_real_read_paths_and_authorize_url(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "github_oauth_client_id", "github-client-id-123")
    monkeypatch.setattr(settings, "github_oauth_client_secret", "github-client-secret-456")
    monkeypatch.setattr(
        settings,
        "github_oauth_redirect_uri",
        "http://127.0.0.1:8765/v1/integrations/oauth/callback",
    )
    patch_provider_clients(monkeypatch, github_provider)

    integration = await install(
        client,
        "github",
        scopes=["github:read", "github:act"],
        config={"provider": "github"},
    )
    integration_id = integration["id"]

    resp = await client.get(
        "/v1/integrations/oauth/authorize",
        params={"integration_id": integration_id},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["authorize_url"].startswith("https://github.com/login/oauth/authorize?")
    assert "scope=repo" in resp.json()["authorize_url"]

    await store_oauth(client, integration_id, token="github-token-123456", refresh="github-refresh-123456")

    resp = await client.post(
        f"/v1/integrations/{integration_id}/actions",
        json={"action": "github.list_issues", "args": {"repo": "owner/repo"}},
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert result["mode"] == "github"
    assert result["issues"][0]["title"] == "Fix the conduit"

    resp = await client.post(
        f"/v1/integrations/{integration_id}/actions",
        json={
            "action": "github.comment_pr",
            "args": {"repo": "owner/repo", "number": 42, "body": "LGTM, ship it"},
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"]["comment"]["id"] == 7
    assert FAKE_GITHUB["comment_body"] == "LGTM, ship it"

    # Bad repo shape and missing comment body are rejected before the network.
    resp = await client.post(
        f"/v1/integrations/{integration_id}/actions",
        json={"action": "github.list_issues", "args": {"repo": "owner"}},
    )
    assert resp.status_code == 400
    resp = await client.post(
        f"/v1/integrations/{integration_id}/actions",
        json={
            "action": "github.comment_pr",
            "args": {"repo": "owner/repo", "number": 42},
        },
    )
    assert resp.status_code == 400

    await _assert_no_secret_leaks(
        db_session,
        ["github-token-123456", "github-refresh-123456", "github-client-secret-456"],
    )


async def test_github_oauth_refresh_rotation(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "github_oauth_client_id", "github-client-id-123")
    monkeypatch.setattr(settings, "github_oauth_client_secret", "github-client-secret-456")
    monkeypatch.setattr(
        settings,
        "github_oauth_redirect_uri",
        "http://127.0.0.1:8765/v1/integrations/oauth/callback",
    )
    patch_provider_clients(monkeypatch, github_provider)

    integration = await install(
        client,
        "github",
        scopes=["github:read", "github:act"],
        config={"provider": "github"},
    )
    integration_id = integration["id"]
    await store_oauth(
        client,
        integration_id,
        token="github-token-123456",
        refresh="github-refresh-123456",
    )

    resp = await client.post(f"/v1/integrations/{integration_id}/credentials/refresh")
    assert resp.status_code == 200, resp.text

    row = (
        await db_session.execute(
            select(IntegrationCredential).where(
                IntegrationCredential.integration_id == UUID(integration_id),
                IntegrationCredential.kind == "oauth",
            )
        )
    ).scalar_one()
    assert row.encrypted_access is not None
    assert row.encrypted_refresh is not None
    assert vault.decrypt(row.encrypted_access) == "github-token-refreshed-789"
    assert vault.decrypt(row.encrypted_refresh) == "github-refresh-rotated-111"
    assert row.token_fingerprint == sha256_hex("github-token-refreshed-789")
    await _assert_no_secret_leaks(
        db_session,
        ["github-refresh-123456", "github-token-refreshed-789", "github-client-secret-456"],
    )


async def test_github_revoke_calls_provider_revoke_and_wipes_vault(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "github_oauth_client_id", "github-client-id-123")
    monkeypatch.setattr(settings, "github_oauth_client_secret", "github-client-secret-456")
    monkeypatch.setattr(
        settings,
        "github_oauth_redirect_uri",
        "http://127.0.0.1:8765/v1/integrations/oauth/callback",
    )
    patch_provider_clients(monkeypatch, github_provider)

    integration = await install(
        client,
        "github",
        scopes=["github:read", "github:act"],
        config={"provider": "github", "revoke_remote": True},
    )
    integration_id = integration["id"]
    await store_oauth(client, integration_id, token="github-token-123456")

    resp = await client.delete(f"/v1/integrations/{integration_id}")
    assert resp.status_code == 200, resp.text
    assert FAKE_GITHUB["revoked_token"] == "github-token-123456"
    assert FAKE_GITHUB["revoked_client_id"] == "github-client-id-123"

    row = (
        await db_session.execute(
            select(IntegrationCredential).where(
                IntegrationCredential.integration_id == UUID(integration_id),
                IntegrationCredential.kind == "oauth",
            )
        )
    ).scalar_one()
    assert row.encrypted_access is None
    assert row.encrypted_refresh is None
    assert row.token_fingerprint is None


async def test_oauth_flow_fails_closed_when_provider_unconfigured(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "google_oauth_client_id", "")
    monkeypatch.setattr(settings, "google_oauth_client_secret", "")
    monkeypatch.setattr(settings, "google_oauth_redirect_uri", "")
    integration = await install(client, "calendar", config={"provider": "google"})
    resp = await client.get(
        "/v1/integrations/oauth/authorize",
        params={"integration_id": integration["id"]},
    )
    assert resp.status_code == 400
    assert "not configured" in resp.json()["detail"]


async def test_callback_rejects_unknown_or_expired_state(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "google_oauth_client_id", "google-client-id.apps.googleusercontent.com")
    monkeypatch.setattr(settings, "google_oauth_client_secret", "google-client-secret-123456")
    monkeypatch.setattr(
        settings,
        "google_oauth_redirect_uri",
        "http://127.0.0.1:8765/v1/integrations/oauth/callback",
    )
    patch_provider_clients(monkeypatch, google_provider)

    integration = await install(client, "calendar", config={"provider": "google"})
    integration_id = integration["id"]
    resp = await client.get(
        "/v1/integrations/oauth/callback",
        params={"code": "fake-auth-code", "state": "not-a-real-state"},
    )
    assert resp.status_code == 400
    assert "unknown" in resp.json()["detail"]

    authorize = (
        await client.get(
            "/v1/integrations/oauth/authorize",
            params={"integration_id": integration_id},
        )
    ).json()
    state_row = (
        await db_session.execute(
            select(IntegrationCredential).where(
                IntegrationCredential.integration_id == UUID(integration_id),
                IntegrationCredential.kind == "oauth_state",
            )
        )
    ).scalar_one()
    state_row.metadata_ = {
        "expires_at": (utcnow() - timedelta(seconds=1)).isoformat()
    }
    await db_session.commit()
    resp = await client.get(
        "/v1/integrations/oauth/callback",
        params={"code": "fake-auth-code", "state": authorize["state"]},
    )
    assert resp.status_code == 400
    assert "expired" in resp.json()["detail"]


async def test_health_batch_webhook_keeps_strict_allowlist(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    health = await install(client, "health")
    health_id = health["id"]
    resp = await client.post(f"/v1/integrations/{health_id}/webhook-secret")
    secret = resp.json()["secret"]

    payload = {
        "metrics": {
            "heart_rate": 72,
            "hrv": 54.2,
            "sleep_hours": 7.5,
            "readiness": 0.82,
            "blood_oxygen": 98,
            "steps": True,
            "sleep_hours_text": "7.5",
        },
        "units": {"heart_rate": "bpm", "hrv": "ms", "sleep_hours": "h"},
    }
    resp = await client.post(
        f"/v1/integrations/webhook/{health_id}",
        content=json.dumps(payload),
        headers=signed_headers(payload, secret),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["accepted"] == 4

    rows = (
        await db_session.execute(
            select(LiveEvent).where(
                LiveEvent.channel_id == UUID(health["live_channel_id"])
            )
        )
    ).scalars().all()
    metrics = {row.payload["metric"]: row.payload for row in rows}
    assert set(metrics) == {"heart_rate", "hrv", "sleep_hours", "readiness"}
    assert metrics["heart_rate"]["value"] == 72
    assert metrics["heart_rate"]["unit"] == "bpm"
    assert metrics["sleep_hours"]["unit"] == "h"
    assert all(row.privacy_level == "sensitive" for row in rows)

    valid_single = {"metric": "steps", "value": 8123, "unit": "count"}
    resp = await client.post(
        f"/v1/integrations/webhook/{health_id}",
        content=json.dumps(valid_single),
        headers=signed_headers(valid_single, secret),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["accepted"] == 1

    # The single-metric contract still works and still rejects bools.
    single = {"metric": "steps", "value": True, "unit": "count"}
    resp = await client.post(
        f"/v1/integrations/webhook/{health_id}",
        content=json.dumps(single),
        headers=signed_headers(single, secret),
    )
    assert resp.json()["accepted"] == 0


def test_calendar_signal_derivation_unit() -> None:
    now = utcnow()
    events = [
        {
            "provider": "google",
            "calendar_id": "primary",
            "event_id": "a",
            "summary": "Standup",
            "start": (now + timedelta(hours=1)).isoformat(),
            "end": (now + timedelta(hours=1, minutes=30)).isoformat(),
            "busy": True,
            "attendees": [
                {"name": "Ada", "email": "ada@example.com", "status": "accepted"}
            ],
        },
        {
            "provider": "google",
            "calendar_id": "primary",
            "event_id": "b",
            "summary": "Focus block",
            "start": (now + timedelta(hours=2)).isoformat(),
            "end": (now + timedelta(hours=3)).isoformat(),
            "busy": False,
        },
    ]
    signals = derive_calendar_signals(events, now=now)
    assert signals["next_event"]["summary"] == "Standup"
    assert signals["next_event"]["participants"][0]["email"] == "ada@example.com"
    assert signals["today"]["event_count"] == 1
    assert signals["today"]["busy_minutes"] == 30
    assert len(signals["day_density"]) == settings.calendar_sync_days + 1
    assert signals["deadline_proximity"] == pytest.approx(1.0 - 1 / 48, abs=0.01)
    assert signals["leave_by"] == (now + timedelta(minutes=30)).isoformat()
    assert signals["quiet_hours"]["start"] == settings.quiet_hours_start


def test_pkce_pair_is_unique_and_s256() -> None:
    verifier, challenge = oauth.new_pkce_pair()
    assert len(verifier) >= 40
    assert challenge == _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    verifier2, challenge2 = oauth.new_pkce_pair()
    assert verifier != verifier2
    assert challenge != challenge2
