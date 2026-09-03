"""Standard integration adapter framework.

Every external system (calendar, health, GitHub, smart home, messaging) is
represented by an :class:`Adapter` that declares its capabilities (scopes),
privacy floor, webhook event types, and permissioned actions. Provider
specifics stay behind this interface: adding or replacing an integration is a
registry/config change, not a rewrite of EV's core systems.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

from app.config import settings
from app.gateway.validation import validate_arguments
from app.integrations import oauth
from app.integrations.calendar_signals import derive_calendar_signals, parse_event_time
from app.integrations.life_helper import (
    LifeHelperError,
    LifeHelperUnavailableError,
    run_life_helper,
)
from app.integrations.life_policy import evaluate_life_policy
from app.schemas import LiveEventCreate
from app.utils.text import utcnow


def _text(value: Any, limit: int) -> str:
    if isinstance(value, str):
        return value[:limit]
    if value is None:
        return ""
    return str(value)[:limit]


def _make_client(timeout: float = 10.0) -> httpx.AsyncClient:
    """Create the provider HTTP client; a seam for tests to inject a mock provider."""
    return oauth.make_http_client(timeout=timeout)


@dataclass(frozen=True)
class AdapterAction:
    name: str
    scope: str
    description: str
    parameters: dict = field(default_factory=dict)


@dataclass(frozen=True)
class AdapterSyncResult:
    """Provider pull outcome: normalized live events plus derived signals."""

    events: list[LiveEventCreate] = field(default_factory=list)
    signals: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Adapter:
    """One external system behind a stable adapter contract."""

    slug: str
    name: str
    description: str
    capabilities: tuple[str, ...]
    default_scopes: tuple[str, ...]
    min_privacy: str = "normal"
    privacy_kind: str = "app"
    event_types: tuple[str, ...] = ()
    actions: tuple[AdapterAction, ...] = ()

    def scopes_ok(self, scopes: list[str]) -> bool:
        return bool(scopes) and set(scopes) <= set(self.capabilities)

    def action(self, name: str) -> AdapterAction | None:
        return next((a for a in self.actions if a.name == name), None)

    async def translate_webhook(
        self,
        payload: dict,
        headers: dict | None = None,
    ) -> list[LiveEventCreate]:
        raise NotImplementedError(f"{self.slug} does not declare a webhook translator")

    async def act(
        self,
        *,
        action: str,
        args: dict,
        token: str,
        scopes: list[str],
        config: dict,
    ) -> dict:
        """Execute one permissioned action for this adapter.

        ``local`` mode is deterministic and provider-free (tests/dev); ``http``
        forwards the action to the configured provider base URL with the vault
        token as a Bearer credential. The token is never logged or returned.
        """
        spec = self.action(action)
        if spec is None:
            raise KeyError(f"unknown action '{action}'")
        if spec.scope not in scopes:
            raise PermissionError(f"scope '{spec.scope}' is not granted")
        effective_args, issues = validate_arguments(args or {}, spec.parameters)
        if issues:
            raise ValueError("; ".join(issues))
        args = effective_args
        if config.get("provider") == "http":
            base_url = config.get("base_url")
            if not base_url:
                raise ValueError("provider=http requires base_url in integration config")
            async with _make_client() as client:
                response = await client.post(
                    f"{base_url.rstrip('/')}/actions/{action}",
                    headers={"Authorization": f"Bearer {token}"},
                    json=args,
                )
                response.raise_for_status()
                return response.json()
        return {"ok": True, "mode": "local", "action": action}

    async def sync(
        self,
        *,
        token: str,
        scopes: list[str],
        config: dict,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> AdapterSyncResult:
        """Pull provider data into normalized live events and derived signals.

        The base implementation is the deterministic offline double: no
        network, no events, and an explicit ``local`` signal. Real providers
        override this method.
        """
        return AdapterSyncResult(signals={"mode": "local"})

    async def refresh_token(
        self,
        *,
        token: str,
        refresh_token: str,
        config: dict,
    ) -> dict:
        """Exchange a refresh token for a new access token (OAuth refresh flow).

        Returns ``{access_token, refresh_token?, expires_at?, token_type?}``.
        Provider-specific implementations live behind this adapter method; the
        plaintext refresh token is never logged or stored outside the vault.
        """
        if config.get("provider") == "http":
            base_url = config.get("base_url")
            if not base_url:
                raise ValueError("provider=http requires base_url in integration config")
            async with _make_client() as client:
                response = await client.post(
                    f"{base_url.rstrip('/')}/oauth/refresh",
                    json={"refresh_token": refresh_token},
                )
                response.raise_for_status()
                data = response.json()
            access_token = data.get("access_token")
            if not isinstance(access_token, str) or len(access_token) < 8:
                raise ValueError("refresh response is missing a valid access_token")
            return {
                "access_token": access_token,
                "refresh_token": data.get("refresh_token") or refresh_token,
                "expires_at": data.get("expires_at"),
                "token_type": data.get("token_type") or "Bearer",
            }
        raise NotImplementedError(
            f"{self.slug} does not support token refresh in local mode"
        )

    async def revoke_remote(
        self,
        *,
        token: str,
        config: dict,
    ) -> dict:
        """Best-effort provider-side revocation of a granted credential.

        Called during integration revocation when ``config.revoke_remote`` is
        set. Local revocation always proceeds even if the provider call fails.
        """
        if config.get("provider") == "http":
            base_url = config.get("base_url")
            if not base_url:
                raise ValueError("provider=http requires base_url in integration config")
            async with _make_client() as client:
                response = await client.post(
                    f"{base_url.rstrip('/')}/oauth/revoke",
                    headers={"Authorization": f"Bearer {token}"},
                )
                response.raise_for_status()
                return response.json()
        return {"ok": True, "mode": "local"}


@dataclass(frozen=True)
class CalendarAdapter(Adapter):
    GOOGLE_READ_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"

    async def act(
        self,
        *,
        action: str,
        args: dict,
        token: str,
        scopes: list[str],
        config: dict,
    ) -> dict:
        if action == "calendar.list_upcoming" and config.get("provider") == "google":
            events = await self._fetch_google_events(token, config)
            return {
                "ok": True,
                "mode": "google",
                "action": action,
                "events": events,
                "signals": derive_calendar_signals(events),
            }
        if action == "calendar.create_event":
            if config.get("provider") == "http":
                return await super().act(
                    action=action,
                    args=args,
                    token=token,
                    scopes=scopes,
                    config=config,
                )
            return await self._create_event(args, token, config)
        return await super().act(
            action=action,
            args=args,
            token=token,
            scopes=scopes,
            config=config,
        )

    async def sync(
        self,
        *,
        token: str,
        scopes: list[str],
        config: dict,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> AdapterSyncResult:
        if config.get("provider") != "google":
            return AdapterSyncResult(signals={"mode": "local"})
        events = await self._fetch_google_events(token, config, since=since, until=until)
        return AdapterSyncResult(
            events=[self._to_live_event(event) for event in events],
            signals=derive_calendar_signals(events),
        )

    async def refresh_token(
        self,
        *,
        token: str,
        refresh_token: str,
        config: dict,
    ) -> dict:
        if config.get("provider") == "google":
            provider = oauth.provider_for("calendar")
            if provider is None:  # pragma: no cover - calendar always maps
                raise oauth.OAuthProviderError("calendar OAuth provider unavailable")
            return await provider.refresh(refresh_token)
        return await super().refresh_token(
            token=token,
            refresh_token=refresh_token,
            config=config,
        )

    async def revoke_remote(
        self,
        *,
        token: str,
        config: dict,
    ) -> dict:
        if config.get("provider") == "google":
            provider = oauth.provider_for("calendar")
            if provider is None:  # pragma: no cover - calendar always maps
                raise oauth.OAuthProviderError("calendar OAuth provider unavailable")
            return await provider.revoke(token)
        return await super().revoke_remote(token=token, config=config)

    async def _create_event(self, args: dict, token: str, config: dict) -> dict:
        title = _text(args.get("summary") or args.get("title"), 256) or "Untitled event"
        if config.get("provider") == "google":
            if not token:
                raise oauth.OAuthAuthError("calendar write requires an access token")
            provider = oauth.provider_for("calendar")
            if provider is None:
                raise oauth.OAuthProviderError("calendar OAuth provider unavailable")
            calendar_id = str(config.get("calendar_id") or "primary")
            body = {
                "summary": title,
                "start": {"dateTime": args.get("start")},
                "end": {"dateTime": args.get("end") or args.get("start")},
            }
            if args.get("location"):
                body["location"] = _text(args.get("location"), 512)
            start_at = parse_event_time(str(args.get("start") or ""))
            if start_at is not None:
                from app.ev.resolve import is_near_duplicate

                try:
                    nearby = await self._fetch_google_events(
                        token,
                        config,
                        since=start_at - timedelta(minutes=15),
                        until=start_at + timedelta(minutes=15),
                    )
                except Exception:
                    nearby = []
                for existing in nearby:
                    if is_near_duplicate(
                        title=title,
                        start=args.get("start"),
                        other_title=str(existing.get("summary") or existing.get("title") or ""),
                        other_start=existing.get("start") or existing.get("starts_at"),
                    ):
                        event_id = _text(existing.get("id"), 256)
                        if event_id:
                            return {
                                "ok": True,
                                "mode": "google",
                                "id": event_id,
                                "event_id": event_id,
                                "duplicate": True,
                                "evidence": {"id": event_id, "duplicate": True},
                                "summary": title,
                            }
            url = f"{provider.api_base}/calendars/{quote(calendar_id, safe='')}/events"
            async with _make_client() as client:
                response = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    json=body,
                )
            if response.status_code in (401, 403):
                raise oauth.OAuthAuthError("calendar provider rejected the access token")
            if response.status_code >= 400:
                raise oauth.OAuthProviderError(
                    f"calendar create failed (status {response.status_code})"
                )
            data = response.json()
            event_id = _text(data.get("id"), 256)
            if not event_id:
                return {"ok": False, "error": "missing_event_id", "mode": "google"}
            return {
                "ok": True,
                "mode": "google",
                "id": event_id,
                "event_id": event_id,
                "evidence": {"id": event_id},
            }
        from uuid import uuid4

        from app.ev.resolve import is_near_duplicate

        events = config.setdefault("events", [])
        if not isinstance(events, list):
            events = []
            config["events"] = events
        start = str(args.get("start") or "")
        for existing in events:
            if not isinstance(existing, dict):
                continue
            if is_near_duplicate(
                title=title,
                start=start,
                other_title=str(existing.get("summary") or existing.get("title") or ""),
                other_start=existing.get("start"),
            ):
                event_id = str(existing.get("id") or "")
                if event_id:
                    return {
                        "ok": True,
                        "mode": "local",
                        "id": event_id,
                        "event_id": event_id,
                        "duplicate": True,
                        "evidence": {"id": event_id, "duplicate": True},
                        "summary": title,
                    }
        event_id = f"local-{uuid4()}"
        events.append(
            {
                "id": event_id,
                "summary": title,
                "start": start,
                "end": args.get("end"),
                "location": args.get("location"),
            }
        )
        return {
            "ok": True,
            "mode": "local",
            "id": event_id,
            "event_id": event_id,
            "evidence": {"id": event_id},
            "summary": title,
        }

    async def _fetch_google_events(
        self,
        token: str,
        config: dict,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[dict]:
        provider = oauth.provider_for("calendar")
        if provider is None:  # pragma: no cover - calendar always maps
            raise oauth.OAuthProviderError("calendar OAuth provider unavailable")
        calendar_id = str(config.get("calendar_id") or "primary")
        now = utcnow()
        params = {
            "timeMin": (since or (now - timedelta(hours=1))).isoformat(),
            "timeMax": (
                until
                or (now + timedelta(days=settings.calendar_sync_days))
            ).isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": str(settings.calendar_sync_max_events),
            "showDeleted": "false",
        }
        url = f"{provider.api_base}/calendars/{quote(calendar_id, safe='')}/events"
        async with _make_client() as client:
            response = await client.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.status_code in (401, 403):
            raise oauth.OAuthAuthError("calendar provider rejected the access token")
        if response.status_code >= 400:
            raise oauth.OAuthProviderError(
                f"calendar provider request failed (status {response.status_code})"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise oauth.OAuthProviderError(
                "calendar provider returned a non-JSON response"
            ) from exc
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise oauth.OAuthProviderError(
                "calendar provider returned a malformed events response"
            )
        return [
            self._normalize_google_event(calendar_id, item)
            for item in items
            if isinstance(item, dict)
        ]

    @staticmethod
    def _normalize_google_event(calendar_id: str, item: dict) -> dict:
        raw_start = item.get("start")
        raw_end = item.get("end")
        start_info: dict[str, Any] = raw_start if isinstance(raw_start, dict) else {}
        end_info: dict[str, Any] = raw_end if isinstance(raw_end, dict) else {}
        start = start_info.get("dateTime") or start_info.get("date")
        end = end_info.get("dateTime") or end_info.get("date")
        attendees: list[dict] = []
        for attendee in item.get("attendees") or []:
            if not isinstance(attendee, dict):
                continue
            attendees.append(
                {
                    "name": _text(attendee.get("displayName"), 128),
                    "email": _text(attendee.get("email"), 256) or None,
                    "status": _text(attendee.get("responseStatus"), 32),
                }
            )
        organizer = item.get("organizer")
        organizer_out = None
        if isinstance(organizer, dict) and organizer:
            organizer_out = {
                "name": _text(organizer.get("displayName"), 128),
                "email": _text(organizer.get("email"), 256) or None,
            }
        return {
            "provider": "google",
            "calendar_id": calendar_id,
            "event_id": _text(item.get("id"), 256),
            "summary": _text(item.get("summary"), 256) or "Untitled event",
            "start": start,
            "end": end,
            "all_day": "date" in start_info,
            "location": _text(item.get("location"), 512) or None,
            "busy": item.get("transparency") != "transparent",
            "status": _text(item.get("status"), 32),
            "organizer": organizer_out,
            "attendees": attendees,
            "hangout_link": _text(item.get("hangoutLink"), 512) or None,
            "html_link": _text(item.get("htmlLink"), 512) or None,
            "source": "calendar",
        }

    @staticmethod
    def _to_live_event(payload: dict) -> LiveEventCreate:
        return LiveEventCreate(
            event_type="calendar.event.updated",
            payload=payload,
            occurred_at=parse_event_time(payload.get("start")),
        )

    async def translate_webhook(
        self,
        payload: dict,
        headers: dict | None = None,
    ) -> list[LiveEventCreate]:
        if not isinstance(payload, dict) or not payload:
            return []
        summary = _text(payload.get("summary"), 256) or "Calendar event"
        return [
            LiveEventCreate(
                event_type="calendar.event.updated",
                payload={
                    "summary": summary,
                    "start": payload.get("start"),
                    "end": payload.get("end"),
                    "calendar_id": _text(payload.get("calendar_id"), 128),
                    "source": "calendar",
                },
            )
        ]


@dataclass(frozen=True)
class HealthAdapter(Adapter):
    async def translate_webhook(
        self,
        payload: dict,
        headers: dict | None = None,
    ) -> list[LiveEventCreate]:
        allowed = {"heart_rate", "hrv", "sleep_hours", "steps", "readiness"}
        events: list[LiveEventCreate] = []
        raw_metrics: object = payload.get("metrics")
        if isinstance(raw_metrics, dict):
            raw_units = payload.get("units")
            units = raw_units if isinstance(raw_units, dict) else {}
            for metric, value in raw_metrics.items():
                if (
                    metric not in allowed
                    or not isinstance(value, (int, float))
                    or isinstance(value, bool)
                ):
                    continue
                events.append(
                    LiveEventCreate(
                        event_type="health.metric.updated",
                        payload={
                            "metric": metric,
                            "value": value,
                            "unit": _text(units.get(metric), 16),
                            "source": "health",
                        },
                    )
                )
            return events
        metric = _text(payload.get("metric"), 64)
        value = payload.get("value")
        if (
            metric not in allowed
            or not isinstance(value, (int, float))
            or isinstance(value, bool)
        ):
            return []
        return [
            LiveEventCreate(
                event_type="health.metric.updated",
                payload={
                    "metric": metric,
                    "value": value,
                    "unit": _text(payload.get("unit"), 16),
                    "source": "health",
                },
            )
        ]


@dataclass(frozen=True)
class GitHubAdapter(Adapter):
    async def act(
        self,
        *,
        action: str,
        args: dict,
        token: str,
        scopes: list[str],
        config: dict,
    ) -> dict:
        if config.get("provider") != "github":
            spec = self.action(action)
            if spec is None:
                raise KeyError(f"unknown action '{action}'")
            if spec.scope not in scopes:
                raise PermissionError(f"scope '{spec.scope}' is not granted")
            if action == "github.list_issues":
                return {
                    "ok": True,
                    "mode": "local",
                    "action": action,
                    "issues": list(config.get("issues") or []),
                    "simulated": True,
                }
            return await super().act(
                action=action,
                args=args,
                token=token,
                scopes=scopes,
                config=config,
            )
        if action == "github.list_issues":
            return await self._list_issues(token, args)
        if action == "github.comment_pr":
            return await self._comment_pr(token, args)
        return await super().act(
            action=action,
            args=args,
            token=token,
            scopes=scopes,
            config=config,
        )

    async def refresh_token(
        self,
        *,
        token: str,
        refresh_token: str,
        config: dict,
    ) -> dict:
        if config.get("provider") == "github":
            provider = oauth.provider_for("github")
            if provider is None:  # pragma: no cover - github always maps
                raise oauth.OAuthProviderError("github OAuth provider unavailable")
            return await provider.refresh(refresh_token)
        return await super().refresh_token(
            token=token,
            refresh_token=refresh_token,
            config=config,
        )

    async def revoke_remote(
        self,
        *,
        token: str,
        config: dict,
    ) -> dict:
        if config.get("provider") == "github":
            provider = oauth.provider_for("github")
            if provider is None:  # pragma: no cover - github always maps
                raise oauth.OAuthProviderError("github OAuth provider unavailable")
            return await provider.revoke(token)
        return await super().revoke_remote(token=token, config=config)

    @staticmethod
    def _repo_args(args: dict) -> tuple[str, str]:
        repo = str(args.get("repo") or "").strip().strip("/")
        parts = repo.split("/")
        if len(parts) != 2 or not all(parts):
            raise ValueError("repo must be 'owner/name'")
        return parts[0], parts[1]

    @staticmethod
    def _github_request_headers(token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _list_issues(self, token: str, args: dict) -> dict:
        owner, repo = self._repo_args(args)
        provider = oauth.provider_for("github")
        if provider is None:  # pragma: no cover - github always maps
            raise oauth.OAuthProviderError("github OAuth provider unavailable")
        limit = args.get("limit")
        per_page = min(max(int(limit) if isinstance(limit, int) else 20, 1), 100)
        params = {
            "state": "open",
            "per_page": str(per_page),
            "sort": "updated",
            "direction": "desc",
        }
        url = f"{provider.api_base}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/issues"
        async with _make_client() as client:
            response = await client.get(
                url,
                params=params,
                headers=self._github_request_headers(token),
            )
        if response.status_code in (401, 403):
            raise oauth.OAuthAuthError("github provider rejected the access token")
        if response.status_code == 404:
            raise oauth.OAuthProviderError(
                "repository not found or not visible to the granted token"
            )
        if response.status_code >= 400:
            raise oauth.OAuthProviderError(
                f"github provider request failed (status {response.status_code})"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise oauth.OAuthProviderError(
                "github provider returned a non-JSON response"
            ) from exc
        if not isinstance(data, list):
            raise oauth.OAuthProviderError(
                "github provider returned a malformed issues response"
            )
        issues = []
        for item in data:
            if not isinstance(item, dict):
                continue
            raw_user = item.get("user")
            user: dict[str, Any] = raw_user if isinstance(raw_user, dict) else {}
            issues.append(
                {
                    "number": item.get("number"),
                    "title": _text(item.get("title"), 256),
                    "state": _text(item.get("state"), 32),
                    "html_url": _text(item.get("html_url"), 512) or None,
                    "created_at": item.get("created_at"),
                    "updated_at": item.get("updated_at"),
                    "author": _text(user.get("login"), 128) or None,
                    "pull_request": item.get("pull_request") is not None,
                    "comments": item.get("comments"),
                }
            )
        return {"ok": True, "mode": "github", "action": "github.list_issues", "issues": issues}

    async def _comment_pr(self, token: str, args: dict) -> dict:
        owner, repo = self._repo_args(args)
        number = args.get("number")
        if not isinstance(number, int) or number <= 0:
            raise ValueError("number must be a positive pull-request number")
        body = str(args.get("body") or "").strip()
        if not body:
            raise ValueError("body must be a non-empty comment")
        provider = oauth.provider_for("github")
        if provider is None:  # pragma: no cover - github always maps
            raise oauth.OAuthProviderError("github OAuth provider unavailable")
        url = (
            f"{provider.api_base}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
            f"/issues/{number}/comments"
        )
        async with _make_client() as client:
            response = await client.post(
                url,
                json={"body": body},
                headers=self._github_request_headers(token),
            )
        if response.status_code in (401, 403):
            raise oauth.OAuthAuthError("github provider rejected the access token")
        if response.status_code >= 400:
            raise oauth.OAuthProviderError(
                f"github provider request failed (status {response.status_code})"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise oauth.OAuthProviderError(
                "github provider returned a non-JSON response"
            ) from exc
        if not isinstance(data, dict):
            raise oauth.OAuthProviderError(
                "github provider returned a malformed comment response"
            )
        return {
            "ok": True,
            "mode": "github",
            "action": "github.comment_pr",
            "comment": {
                "id": data.get("id"),
                "html_url": _text(data.get("html_url"), 512) or None,
                "created_at": data.get("created_at"),
            },
        }

    async def translate_webhook(
        self,
        payload: dict,
        headers: dict | None = None,
    ) -> list[LiveEventCreate]:
        if not isinstance(payload, dict):
            return []
        events: list[LiveEventCreate] = []
        repo = payload.get("repository") or {}
        repo_name = _text(repo.get("full_name") or repo.get("name"), 128)
        run = payload.get("workflow_run")
        if (
            isinstance(run, dict)
            and payload.get("action") == "completed"
            and run.get("conclusion") == "failure"
        ):
            events.append(
                LiveEventCreate(
                    event_type="github.ci.failure",
                    payload={
                        "repository": repo_name,
                        "workflow": _text(run.get("name"), 128),
                        "run_id": run.get("id"),
                        "conclusion": "failure",
                        "source": "github",
                    },
                )
            )
        issue = payload.get("issue")
        if isinstance(issue, dict):
            events.append(
                LiveEventCreate(
                    event_type="github.issue.updated",
                    payload={
                        "repository": repo_name,
                        "number": issue.get("number"),
                        "title": _text(issue.get("title"), 256),
                        "action": _text(payload.get("action"), 32),
                        "source": "github",
                    },
                )
            )
        pull_request = payload.get("pull_request")
        if isinstance(pull_request, dict):
            events.append(
                LiveEventCreate(
                    event_type="github.pr.updated",
                    payload={
                        "repository": repo_name,
                        "number": pull_request.get("number"),
                        "title": _text(pull_request.get("title"), 256),
                        "action": _text(payload.get("action"), 32),
                        "source": "github",
                    },
                )
            )
        return events


@dataclass(frozen=True)
class SmartHomeAdapter(Adapter):
    async def act(
        self,
        *,
        action: str,
        args: dict,
        token: str,
        scopes: list[str],
        config: dict,
    ) -> dict:
        spec = self.action(action)
        if spec is None:
            raise KeyError(f"unknown action '{action}'")
        if spec.scope not in scopes:
            raise PermissionError(f"scope '{spec.scope}' is not granted")
        from app.ev.home import adapter_act

        return await adapter_act(
            action=action,
            args=args or {},
            token=token,
            scopes=scopes,
            config=config,
            session=config.get("_session"),
        )

    async def sync(
        self,
        *,
        token: str,
        scopes: list[str],
        config: dict,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> AdapterSyncResult:
        if config.get("provider") == "homeassistant":
            from app.ev.home import sync_from_ha

            session = config.get("_session")
            count = 0
            if session is not None:
                count = await sync_from_ha(session, token=token, config=config)
            return AdapterSyncResult(signals={"mode": "homeassistant", "synced": count})
        return AdapterSyncResult(signals={"mode": "local", "simulated": True})

    async def translate_webhook(
        self,
        payload: dict,
        headers: dict | None = None,
    ) -> list[LiveEventCreate]:
        if not isinstance(payload, dict):
            return []
        device = _text(payload.get("device_id") or payload.get("device"), 128)
        if not device:
            return []
        state = payload.get("state")
        if not isinstance(state, (str, int, float, bool)):
            state = None
        attributes = payload.get("attributes")
        if not isinstance(attributes, dict):
            attributes = None
        return [
            LiveEventCreate(
                event_type="home.device.updated",
                payload={
                    "device_id": device,
                    "state": state,
                    "attributes": attributes,
                    "source": "smart_home",
                },
            )
        ]


def _life_action_common(
    *,
    action: str,
    args: dict,
    scopes: list[str],
    config: dict,
) -> tuple[dict, dict]:
    """Shared macos_life policy gate + helper args for send/call actions."""
    decision = evaluate_life_policy(
        scopes=scopes,
        action=action,
        recipient=str(args.get("to") or "") or None,
        contact=args.get("contact") if isinstance(args.get("contact"), dict) else None,
        confirm=bool(args.get("confirm")),
        allowlist=config.get("contact_allowlist"),
        autonomy=config.get("autonomy"),
        confirm_unknown=config.get("confirm_unknown"),
    )
    if not decision.allowed:
        raise PermissionError(decision.reason)
    helper_args = {key: value for key, value in args.items() if key not in ("confirm", "contact")}
    return helper_args, decision.to_dict()


def _life_read_only_action(*, provider: object) -> None:
    if provider != "macos_life":
        raise LifeHelperUnavailableError(
            "no life provider configured for this read action; set "
            "EV_LIFE_HELPER_PATH and provider=macos_life"
        )


@dataclass(frozen=True)
class MessagingAdapter(Adapter):
    async def act(
        self,
        *,
        action: str,
        args: dict,
        token: str,
        scopes: list[str],
        config: dict,
    ) -> dict:
        provider = config.get("provider")
        if provider == "macos_life":
            return await self._macos_life_act(action, args, scopes, config)
        if provider == "device_proxy":
            raise LifeHelperError(
                "device_proxy actions are queued by the integrations service",
                error_code="device_proxy_service_only",
            )
        if provider == "http":
            return await super().act(
                action=action,
                args=args,
                token=token,
                scopes=scopes,
                config=config,
            )
        # Offline double: reads are honest empties; sends NEVER fake success.
        if action == "messaging.send":
            raise LifeHelperUnavailableError(
                "no life provider configured (set EV_MESSAGING_PROVIDER=macos_life "
                "and EV_LIFE_HELPER_PATH); refusing to fake a sent message"
            )
        return {"ok": True, "mode": "local", "action": action, "messages": []}

    async def _macos_life_act(
        self,
        action: str,
        args: dict,
        scopes: list[str],
        config: dict,
    ) -> dict:
        command = {
            "messaging.list_messages": "messages.list",
            "messaging.send": "messages.send",
        }[action]
        if action == "messaging.send":
            helper_args, policy = _life_action_common(
                action=action,
                args=args,
                scopes=scopes,
                config=config,
            )
        else:
            helper_args = {key: value for key, value in args.items() if key != "confirm"}
            policy = {"allowed": True, "confirmation_required": False, "reason": "read"}
        result = await run_life_helper(
            command,
            helper_args,
            helper_path=config.get("helper_path"),
        )
        return {
            "ok": True,
            "mode": "macos_life",
            "action": action,
            **result.data,
            "delivery": result.delivery,
            "policy": policy,
        }

    async def translate_webhook(
        self,
        payload: dict,
        headers: dict | None = None,
    ) -> list[LiveEventCreate]:
        if not isinstance(payload, dict):
            return []
        text = _text(payload.get("text"), 2000)
        if not text:
            return []
        return [
            LiveEventCreate(
                event_type="message.received",
                payload={
                    "sender": _text(payload.get("sender"), 128) or "unknown",
                    "channel": _text(payload.get("channel"), 128) or "unknown",
                    "text": text,
                    "source": "messaging",
                },
            )
        ]


@dataclass(frozen=True)
class ContactsAdapter(Adapter):
    async def act(
        self,
        *,
        action: str,
        args: dict,
        token: str,
        scopes: list[str],
        config: dict,
    ) -> dict:
        provider = config.get("provider")
        if provider in {None, "local"}:
            spec = self.action(action)
            if spec is None:
                raise KeyError(f"unknown action '{action}'")
            if spec.scope not in scopes:
                raise PermissionError(f"scope '{spec.scope}' is not granted")
            if "contacts" not in config or not isinstance(config["contacts"], list):
                config["contacts"] = []
            contacts = config["contacts"]
            if action == "contacts.list":
                return {
                    "ok": True,
                    "mode": "local",
                    "action": action,
                    "contacts": contacts,
                    "simulated": True,
                }
            if action == "contacts.resolve":
                from app.ev.resolve import pick_unique

                query = str(args.get("query") or args.get("name") or "")
                match = pick_unique(
                    query,
                    contacts,
                    labels=lambda row: [
                        str(row.get("name") or ""),
                        str(row.get("display_name") or ""),
                        str(row.get("nickname") or ""),
                        str(row.get("phone") or ""),
                        str(row.get("email") or ""),
                    ],
                )
                if match.status == "ambiguous":
                    return {
                        "ok": False,
                        "mode": "local",
                        "action": action,
                        "error": "ambiguous",
                        "contacts": list(match.candidates),
                        "simulated": True,
                    }
                contact = match.item if match.unique else None
                return {
                    "ok": bool(contact),
                    "mode": "local",
                    "action": action,
                    "contact": contact,
                    "contacts": [contact] if contact else [],
                    "simulated": True,
                    "error": None if contact else "not_found",
                }
            if action == "contacts.create":
                from uuid import uuid4

                new_contact = {
                    "id": f"local-{uuid4()}",
                    "name": str(args.get("name") or ""),
                    "full_name": str(args.get("name") or ""),
                    "phone": str(args.get("phone") or ""),
                    "email": str(args.get("email") or ""),
                    "company": str(args.get("company") or ""),
                }
                contacts.append(new_contact)
                return {
                    "ok": True,
                    "mode": "local",
                    "action": action,
                    "contact": new_contact,
                    "created": True,
                    "simulated": True,
                }
            if action == "contacts.update":
                cid = str(args.get("id") or "")
                query = str(args.get("query") or args.get("name") or "").lower()
                target = None
                for c in contacts:
                    if cid and c.get("id") == cid:
                        target = c
                        break
                    if query and query in str(c.get("name") or c.get("full_name") or "").lower():
                        target = c
                        break
                if not target:
                    target = {"id": cid or "local-updated", "name": str(args.get("name") or query)}
                    contacts.append(target)
                if args.get("name"):
                    target["name"] = str(args["name"])
                    target["full_name"] = str(args["name"])
                if args.get("phone"):
                    target["phone"] = str(args["phone"])
                if args.get("email"):
                    target["email"] = str(args["email"])
                if args.get("company"):
                    target["company"] = str(args["company"])
                return {
                    "ok": True,
                    "mode": "local",
                    "action": action,
                    "contact": target,
                    "updated": True,
                    "simulated": True,
                }
            raise KeyError(f"unknown action '{action}'")
        if action not in {"contacts.resolve", "contacts.list", "contacts.create", "contacts.update"}:
            raise KeyError(f"unknown action '{action}'")
        if action in {"contacts.create", "contacts.update"}:
            if "contacts:act" not in scopes:
                raise PermissionError("scope 'contacts:act' is not granted")
        else:
            if "contacts:read" not in scopes:
                raise PermissionError("scope 'contacts:read' is not granted")
        if config.get("provider") != "macos_life":
            raise LifeHelperUnavailableError(
                "no life provider configured for contacts; set EV_LIFE_HELPER_PATH and provider=macos_life"
            )
        command = {
            "contacts.resolve": "contacts.resolve",
            "contacts.list": "contacts.list",
            "contacts.create": "contacts.create",
            "contacts.update": "contacts.update",
        }[action]
        result = await run_life_helper(
            command,
            args,
            helper_path=config.get("helper_path"),
        )
        return {
            "ok": True,
            "mode": "macos_life",
            "action": action,
            **result.data,
            "delivery": result.delivery,
        }


@dataclass(frozen=True)
class PhoneAdapter(Adapter):
    async def act(
        self,
        *,
        action: str,
        args: dict,
        token: str,
        scopes: list[str],
        config: dict,
    ) -> dict:
        provider = config.get("provider")
        if provider in {None, "local"}:
            spec = self.action(action)
            if spec is None:
                raise KeyError(f"unknown action '{action}'")
            if spec.scope not in scopes:
                raise PermissionError(f"scope '{spec.scope}' is not granted")
            destination = str(args.get("to") or args.get("destination") or "")
            opened = bool(config.get("simulate_opened"))
            return {
                "ok": opened,
                "mode": "local",
                "action": action,
                "opened": opened,
                "simulated": True,
                "error": None if opened else "not_opened",
                "delivery": {
                    "evidence": {
                        "opened": opened,
                        "source": "local",
                        "recipient": destination,
                    }
                },
            }
        if provider == "macos_life":
            helper_args, policy = _life_action_common(
                action=action,
                args=args,
                scopes=scopes,
                config=config,
            )
            command = "call.place"
            destination = helper_args.pop("to", None)
            if not destination:
                raise ValueError("phone call requires a destination")
            helper_args["destination"] = destination
            helper_args["kind"] = "facetime" if action == "facetime.call" else "tel"
            helper_args.pop("video", None)
            result = await run_life_helper(
                command,
                helper_args,
                helper_path=config.get("helper_path"),
            )
            return {
                "ok": True,
                "mode": "macos_life",
                "action": action,
                **result.data,
                "delivery": result.delivery,
                "policy": policy,
            }
        if provider == "device_proxy":
            raise LifeHelperError(
                "device_proxy actions are queued by the integrations service",
                error_code="device_proxy_service_only",
            )
        if provider == "http":
            return await super().act(
                action=action,
                args=args,
                token=token,
                scopes=scopes,
                config=config,
            )
        raise LifeHelperUnavailableError(
            "no life provider configured for phone calls; set EV_LIFE_HELPER_PATH "
            "and provider=macos_life (or device_proxy)"
        )


@dataclass(frozen=True)
class MailAdapter(Adapter):
    async def act(
        self,
        *,
        action: str,
        args: dict,
        token: str,
        scopes: list[str],
        config: dict,
    ) -> dict:
        provider = config.get("provider")
        if provider in {None, "local"}:
            spec = self.action(action)
            if spec is None:
                raise KeyError(f"unknown action '{action}'")
            if spec.scope not in scopes:
                raise PermissionError(f"scope '{spec.scope}' is not granted")
            if action == "mail.list":
                items = list(config.get("inbox") or [])
                return {
                    "ok": True,
                    "mode": "local",
                    "action": action,
                    "items": items,
                    "simulated": True,
                }
            if action == "mail.draft":
                return {
                    "ok": True,
                    "mode": "local",
                    "action": action,
                    "drafted": True,
                    "sent": False,
                    "simulated": True,
                }
            if action == "mail.send":
                if not args.get("confirm"):
                    return {
                        "ok": False,
                        "mode": "local",
                        "sent": False,
                        "error": "confirm_required",
                    }
                from uuid import uuid4

                message_id = f"local-{uuid4()}"
                return {
                    "ok": True,
                    "mode": "local",
                    "action": action,
                    "sent": True,
                    "simulated": True,
                    "message_id": message_id,
                }
            raise KeyError(f"unknown action '{action}'")
        if provider == "google":
            return await self._act_google(
                action=action, args=args, token=token, scopes=scopes, config=config
            )
        _life_read_only_action(provider=provider)
        command = {
            "mail.list": "mail.list",
            "mail.send": "mail.send",
        }[action]
        if action == "mail.send":
            helper_args, policy = _life_action_common(
                action=action,
                args=args,
                scopes=scopes,
                config=config,
            )
        else:
            helper_args = args
            policy = {"allowed": True, "confirmation_required": False, "reason": "read"}
        result = await run_life_helper(
            command,
            helper_args,
            helper_path=config.get("helper_path"),
        )
        return {
            "ok": True,
            "mode": "macos_life",
            "action": action,
            **result.data,
            "delivery": result.delivery,
            "policy": policy,
        }

    async def refresh_token(
        self,
        *,
        token: str,
        refresh_token: str,
        config: dict,
    ) -> dict:
        if config.get("provider") == "google":
            provider = oauth.provider_for("mail")
            if provider is None:  # pragma: no cover - mail always maps
                raise oauth.OAuthProviderError("mail OAuth provider unavailable")
            return await provider.refresh(refresh_token)
        return await super().refresh_token(
            token=token,
            refresh_token=refresh_token,
            config=config,
        )

    async def revoke_remote(
        self,
        *,
        token: str,
        config: dict,
    ) -> dict:
        if config.get("provider") == "google":
            provider = oauth.provider_for("mail")
            if provider is None:  # pragma: no cover - mail always maps
                raise oauth.OAuthProviderError("mail OAuth provider unavailable")
            return await provider.revoke(token)
        return await super().revoke_remote(token=token, config=config)

    async def _act_google(
        self,
        *,
        action: str,
        args: dict,
        token: str,
        scopes: list[str],
        config: dict,
    ) -> dict:
        spec = self.action(action)
        if spec is None:
            raise KeyError(f"unknown action '{action}'")
        if spec.scope not in scopes:
            raise PermissionError(f"scope '{spec.scope}' is not granted")
        if action == "mail.list":
            limit = args.get("limit") or 15
            try:
                limit_n = int(limit)
            except (TypeError, ValueError):
                limit_n = 15
            items = await self._list_gmail(token, config, limit=limit_n)
            return {
                "ok": True,
                "mode": "google",
                "action": action,
                "items": items,
            }
        return {
            "ok": False,
            "mode": "google",
            "action": action,
            "sent": False,
            "error": "gmail_send_not_enabled",
        }

    async def _list_gmail(
        self,
        token: str,
        config: dict,
        *,
        limit: int = 15,
    ) -> list[dict]:
        """Inbox envelopes only: Subject/From/Date. Never bodies."""
        del config
        provider = oauth.provider_for("mail")
        if provider is None:  # pragma: no cover - mail always maps
            raise oauth.OAuthProviderError("mail OAuth provider unavailable")
        if not token:
            raise oauth.OAuthAuthError("gmail list requires an access token")
        cap = max(1, min(int(limit or 15), 15))
        list_url = f"{provider.api_base}/users/me/messages"
        envelopes: list[dict] = []
        async with _make_client() as client:
            listed = await client.get(
                list_url,
                params={"maxResults": str(cap), "q": "in:inbox"},
                headers={"Authorization": f"Bearer {token}"},
            )
            if listed.status_code in (401, 403):
                raise oauth.OAuthAuthError("gmail provider rejected the access token")
            if listed.status_code >= 400:
                raise oauth.OAuthProviderError(
                    f"gmail list failed (status {listed.status_code})"
                )
            try:
                payload = listed.json()
            except ValueError as exc:
                raise oauth.OAuthProviderError(
                    "gmail provider returned a non-JSON list response"
                ) from exc
            rows = payload.get("messages") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                return []
            for row in rows[:cap]:
                if not isinstance(row, dict):
                    continue
                message_id = str(row.get("id") or "").strip()
                if not message_id:
                    continue
                meta = await client.get(
                    f"{provider.api_base}/users/me/messages/{quote(message_id, safe='')}",
                    params=[
                        ("format", "metadata"),
                        ("metadataHeaders", "Subject"),
                        ("metadataHeaders", "From"),
                        ("metadataHeaders", "Date"),
                    ],
                    headers={"Authorization": f"Bearer {token}"},
                )
                if meta.status_code in (401, 403):
                    raise oauth.OAuthAuthError("gmail provider rejected the access token")
                if meta.status_code >= 400:
                    continue
                try:
                    body = meta.json()
                except ValueError:
                    continue
                envelopes.append(self._normalize_gmail_metadata(message_id, body))
        return envelopes

    @staticmethod
    def _normalize_gmail_metadata(message_id: str, body: object) -> dict:
        payload = body.get("payload") if isinstance(body, dict) else None
        headers = payload.get("headers") if isinstance(payload, dict) else None
        subject = ""
        sender = ""
        received = ""
        if isinstance(headers, list):
            for header in headers:
                if not isinstance(header, dict):
                    continue
                name = str(header.get("name") or "").strip().lower()
                value = str(header.get("value") or "").strip()
                if name == "subject":
                    subject = value[:256]
                elif name == "from":
                    sender = value[:256]
                elif name == "date":
                    received = value[:128]
        text = f"{subject} from {sender}".strip()
        return {
            "id": message_id,
            "subject": subject,
            "sender": sender,
            "received": received,
            "text": text,
        }


@dataclass(frozen=True)
class DeviceProxyAdapter(Adapter):
    """iPhone actuator contract: outbound actions are queued for devices.

    The integrations service persists the queue (``LifeOutboundAction``) and
    accepts authenticated device result posts; this adapter only validates
    actions and derives the queue payload so the contract stays testable
    without the database.
    """

    async def act(
        self,
        *,
        action: str,
        args: dict,
        token: str,
        scopes: list[str],
        config: dict,
    ) -> dict:
        raise LifeHelperError(
            "device_proxy actions must go through the integrations service "
            "queue (POST /v1/integrations/{id}/actions)",
            error_code="device_proxy_service_only",
        )

    async def queue_payload(
        self,
        *,
        action: str,
        args: dict,
        scopes: list[str],
        config: dict,
        device_id: str | None = None,
    ) -> dict:
        helper_args, policy = _life_action_common(
            action=action,
            args=args,
            scopes=scopes,
            config=config,
        )
        return {
            "ok": True,
            "mode": "device_proxy",
            "action": action,
            "queued": True,
            "args": helper_args,
            "target_device": device_id,
            "delivery": {"confirmed": False, "status": "queued"},
            "policy": policy,
        }


SEARCH_RESULT_FIELDS: dict[str, tuple[str, ...]] = {
    "url": ("url", "link", "href"),
    "title": ("title", "name", "headline"),
    "snippet": ("snippet", "description", "summary", "text"),
    "published_at": ("published_at", "published", "date", "publishedAt"),
}

SEARCH_RESULT_LIMIT = 20
SEARCH_TEXT_LIMIT = 500


def normalize_search_results(raw: list | None) -> dict:
    """Canonical cited-result shape: bounded, sanitized, deterministic.

    Provider payloads vary (url/link/href, title/name, snippet/description),
    so every result is normalized to ``{title, url, snippet, published_at}``,
    restricted to http(s) URLs, length-bounded, capped at 20 results, and
    accompanied by a numbered citation list for traceable quoting.
    """
    normalized: list[dict] = []
    for item in (raw or [])[:SEARCH_RESULT_LIMIT]:
        if not isinstance(item, dict):
            continue
        result: dict = {"title": "", "url": "", "snippet": "", "published_at": None}
        for result_field, keys in SEARCH_RESULT_FIELDS.items():
            for key in keys:
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    result[result_field] = value.strip()[:SEARCH_TEXT_LIMIT]
                    break
        url = result["url"]
        if not url.startswith(("http://", "https://")):
            continue
        normalized.append(result)
    return {
        "results": normalized,
        "citations": [
            {"index": index, "title": result["title"] or result["url"], "url": result["url"]}
            for index, result in enumerate(normalized, start=1)
        ],
        "result_count": len(normalized),
    }


@dataclass(frozen=True)
class SearchAdapter(Adapter):
    """Permissioned web/research search behind the standard adapter contract."""

    async def act(
        self,
        *,
        action: str,
        args: dict,
        token: str,
        scopes: list[str],
        config: dict,
    ) -> dict:
        result = await super().act(
            action=action,
            args=args,
            token=token,
            scopes=scopes,
            config=config,
        )
        if result.get("mode") == "local":
            return {
                "ok": True,
                "mode": "local",
                "action": action,
                "query": args.get("query"),
                **normalize_search_results([]),
            }
        return {**result, **normalize_search_results(result.get("results"))}


class OctoPrintAdapter(Adapter):
    """Local fake job or OctoPrint/Moonraker REST (key in vault)."""

    async def act(self, *, action: str, args: dict, token: str, scopes: list[str], config: dict) -> dict:
        spec = self.action(action)
        if spec is None:
            raise KeyError(f"unknown action '{action}'")
        if spec.scope not in scopes:
            raise PermissionError(f"scope '{spec.scope}' is not granted")
        if config.get("connected") is False:
            return {"ok": False, "error": "no printer connected", "mode": "local"}
        if action == "octoprint.ping":
            if config.get("provider") == "http" and config.get("base_url"):
                try:
                    async with _make_client(timeout=3.0) as client:
                        resp = await client.get(f"{str(config['base_url']).rstrip('/')}/api/version")
                        resp.raise_for_status()
                    return {"ok": True, "mode": "http", "status": "ok"}
                except Exception as exc:  # noqa: BLE001
                    return {"ok": False, "mode": "http", "error": str(exc)}
            return {"ok": True, "mode": "local", "status": "ok"}
        if action == "octoprint.start":
            return {
                "ok": True,
                "mode": config.get("provider") or "local",
                "vendor_job_id": f"local-{args.get('job_id') or 'job'}",
                "status": "printing",
            }
        if action == "octoprint.status":
            return {
                "ok": True,
                "mode": "local",
                "status": config.get("job_status") or "done",
                "filament_remaining_g": config.get("filament_remaining_g", 120.0),
            }
        return {"ok": True, "mode": "local", "action": action}


class CameraAdapter(Adapter):
    """Owner-registered cameras only. Never scan the LAN."""

    async def act(self, *, action: str, args: dict, token: str, scopes: list[str], config: dict) -> dict:
        spec = self.action(action)
        if spec is None:
            raise KeyError(f"unknown action '{action}'")
        if spec.scope not in scopes:
            raise PermissionError(f"scope '{spec.scope}' is not granted")
        if action == "cameras.clip":
            return {
                "ok": True,
                "mode": "local",
                "camera": args.get("camera"),
                "at": args.get("at"),
                "discovered_lan": False,
                "blob_id": config.get("clip_attachment_id"),
            }
        if action == "cameras.list":
            return {"ok": True, "cameras": list(config.get("cameras") or []), "discovered_lan": False}
        return {"ok": True, "mode": "local", "action": action, "discovered_lan": False}


class DroneAdapter(Adapter):
    """Owner-paired sim or Tello/MAVLink. Takeoff/hover/land/rtl only."""

    async def act(self, *, action: str, args: dict, token: str, scopes: list[str], config: dict) -> dict:
        spec = self.action(action)
        if spec is None:
            raise KeyError(f"unknown action '{action}'")
        if spec.scope not in scopes:
            raise PermissionError(f"scope '{spec.scope}' is not granted")
        cmd = action.split(".", 1)[-1]
        return {
            "ok": True,
            "mode": config.get("provider") or "local",
            "sim": (config.get("provider") or "local") == "local",
            "status": cmd,
            "command": cmd,
        }


class PublicFeedsAdapter(Adapter):
    """Owner-picked RSS/NWS public feeds. Not a private scanner."""

    async def act(self, *, action: str, args: dict, token: str, scopes: list[str], config: dict) -> dict:
        spec = self.action(action)
        if spec is None:
            raise KeyError(f"unknown action '{action}'")
        if spec.scope not in scopes:
            raise PermissionError(f"scope '{spec.scope}' is not granted")
        items = list(config.get("items") or args.get("items") or [])
        return {"ok": True, "mode": "local", "items": items, "url": args.get("url")}


BUILTIN_ADAPTERS: tuple[Adapter, ...] = (
    CalendarAdapter(
        slug="calendar",
        name="Calendar",
        description="Read and manage the user's calendar (events, deadlines, availability).",
        capabilities=("calendar:read", "calendar:act"),
        default_scopes=("calendar:read",),
        min_privacy="normal",
        privacy_kind="app",
        event_types=("calendar.event.updated",),
        actions=(
            AdapterAction("calendar.list_upcoming", "calendar:read", "List upcoming calendar events"),
            AdapterAction(
                "calendar.create_event",
                "calendar:act",
                "Create a calendar event",
                parameters={
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string"},
                        "start": {"type": "string"},
                        "end": {"type": "string"},
                    },
                    "required": ["summary", "start"],
                },
            ),
        ),
    ),
    HealthAdapter(
        slug="health",
        name="Health",
        description="Permissioned personal health signals (HR, HRV, sleep, activity).",
        capabilities=("health:read",),
        default_scopes=("health:read",),
        min_privacy="sensitive",
        privacy_kind="health",
        event_types=("health.metric.updated",),
        actions=(),
    ),
    GitHubAdapter(
        slug="github",
        name="GitHub",
        description="Repo, issue, PR, and CI signals with read and act scopes.",
        capabilities=("github:read", "github:act"),
        default_scopes=("github:read",),
        min_privacy="normal",
        privacy_kind="app",
        event_types=("github.ci.failure", "github.issue.updated", "github.pr.updated"),
        actions=(
            AdapterAction(
                "github.list_issues",
                "github:read",
                "List open issues for a repository",
                parameters={
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    },
                    "required": ["repo"],
                },
            ),
            AdapterAction(
                "github.comment_pr",
                "github:act",
                "Comment on a pull request",
                parameters={
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string", "pattern": r"^[^/\s]+/[^/\s]+$"},
                        "number": {"type": "integer", "minimum": 1},
                        "body": {"type": "string", "minLength": 1, "maxLength": 65536},
                    },
                    "required": ["repo", "number", "body"],
                },
            ),
        ),
    ),
    SmartHomeAdapter(
        slug="smart_home",
        name="Smart Home",
        description="Device state and control for the user's smart home.",
        capabilities=("home:read", "home:act"),
        default_scopes=("home:read",),
        min_privacy="normal",
        privacy_kind="app",
        event_types=("home.device.updated",),
        actions=(
            AdapterAction("home.list_devices", "home:read", "List smart-home devices and state"),
            AdapterAction("home.status", "home:read", "Read home entity state"),
            AdapterAction(
                "home.set_device",
                "home:act",
                "Set a device state",
                parameters={
                    "type": "object",
                    "properties": {
                        "entity": {"type": "string"},
                        "entity_id": {"type": "string"},
                        "action": {"type": "string"},
                        "state": {"type": "string"},
                        "confirm": {"type": "boolean"},
                    },
                },
            ),
            AdapterAction("light.set", "home:act", "Set a light on or off"),
            AdapterAction("lock.set", "home:act", "Lock or unlock"),
            AdapterAction("cover.set", "home:act", "Open or close a cover"),
        ),
    ),
    MessagingAdapter(
        slug="messaging",
        name="Messaging",
        description="Receive and send messages through the user's messaging channels.",
        capabilities=("messaging:read", "messaging:act"),
        default_scopes=("messaging:read",),
        min_privacy="normal",
        privacy_kind="app",
        event_types=("message.received",),
        actions=(
            AdapterAction(
                "messaging.list_messages",
                "messaging:read",
                "List recent messages",
                parameters={
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                        "conversation_id": {"type": "string", "maxLength": 256},
                    },
                },
            ),
            AdapterAction(
                "messaging.send",
                "messaging:act",
                "Send a message (provider must return delivery evidence)",
                parameters={
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "minLength": 1, "maxLength": 256},
                        "text": {"type": "string", "minLength": 1, "maxLength": 4000},
                        "service": {"type": "string", "maxLength": 64},
                        "contact": {"type": "object"},
                        "confirm": {"type": "boolean"},
                    },
                    "required": ["to", "text"],
                },
            ),
        ),
    ),
    ContactsAdapter(
        slug="contacts",
        name="Contacts",
        description="Resolve, list, create, and update the owner's Apple contacts.",
        capabilities=("contacts:read", "contacts:act"),
        default_scopes=("contacts:read",),
        min_privacy="normal",
        privacy_kind="app",
        event_types=(),
        actions=(
            AdapterAction(
                "contacts.resolve",
                "contacts:read",
                "Resolve a name/query to contact records",
                parameters={
                    "type": "object",
                    "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 256}},
                    "required": ["query"],
                },
            ),
            AdapterAction("contacts.list", "contacts:read", "List all contacts"),
            AdapterAction(
                "contacts.create",
                "contacts:act",
                "Create a new contact",
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "minLength": 1, "maxLength": 256},
                        "phone": {"type": "string", "maxLength": 64},
                        "email": {"type": "string", "maxLength": 256},
                        "company": {"type": "string", "maxLength": 256},
                    },
                    "required": ["name"],
                },
            ),
            AdapterAction(
                "contacts.update",
                "contacts:act",
                "Update an existing contact",
                parameters={
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "maxLength": 256},
                        "query": {"type": "string", "maxLength": 256},
                        "name": {"type": "string", "maxLength": 256},
                        "phone": {"type": "string", "maxLength": 64},
                        "email": {"type": "string", "maxLength": 256},
                        "company": {"type": "string", "maxLength": 256},
                    },
                },
            ),
        ),
    ),
    PhoneAdapter(
        slug="phone",
        name="Phone",
        description="Place real phone and FaceTime calls through EVLifeHelper.",
        capabilities=("phone:act",),
        default_scopes=("phone:act",),
        min_privacy="normal",
        privacy_kind="app",
        event_types=(),
        actions=(
            AdapterAction(
                "phone.call",
                "phone:act",
                "Place a phone call",
                parameters={
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "minLength": 1, "maxLength": 256},
                        "contact": {"type": "object"},
                        "confirm": {"type": "boolean"},
                    },
                    "required": ["to"],
                },
            ),
            AdapterAction(
                "facetime.call",
                "phone:act",
                "Place a FaceTime call",
                parameters={
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "minLength": 1, "maxLength": 256},
                        "video": {"type": "boolean"},
                        "contact": {"type": "object"},
                        "confirm": {"type": "boolean"},
                    },
                    "required": ["to"],
                },
            ),
        ),
    ),
    MailAdapter(
        slug="mail",
        name="Mail",
        description="Read Apple Mail locally, or Gmail metadata (subject/sender) via OAuth.",
        capabilities=("mail:read", "mail:act"),
        default_scopes=("mail:read",),
        min_privacy="normal",
        privacy_kind="app",
        event_types=(),
        actions=(
            AdapterAction(
                "mail.list",
                "mail:read",
                "List recent mail messages",
                parameters={
                    "type": "object",
                    "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 200}},
                },
            ),
            AdapterAction(
                "mail.send",
                "mail:act",
                "Send mail (provider must return delivery evidence)",
                parameters={
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "minLength": 1, "maxLength": 256},
                        "subject": {"type": "string", "maxLength": 256},
                        "body": {"type": "string", "maxLength": 4000},
                        "confirm": {"type": "boolean"},
                    },
                    "required": ["to", "body"],
                },
            ),
            AdapterAction(
                "mail.draft",
                "mail:act",
                "Save a mail draft without sending",
                parameters={
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "maxLength": 256},
                        "subject": {"type": "string", "maxLength": 256},
                        "body": {"type": "string", "maxLength": 4000},
                    },
                },
            ),
        ),
    ),
    DeviceProxyAdapter(
        slug="device_proxy",
        name="iPhone Device Proxy",
        description=(
            "Queue outbound messages/calls for registered iPhones and accept "
            "authenticated delivery results (SUIT actuator contract)."
        ),
        capabilities=("messaging:read", "messaging:act", "phone:act", "contacts:read"),
        default_scopes=("messaging:act", "phone:act"),
        min_privacy="normal",
        privacy_kind="app",
        event_types=(),
        actions=(
            AdapterAction(
                "messaging.send",
                "messaging:act",
                "Queue a message for a registered iPhone",
                parameters={
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "minLength": 1, "maxLength": 256},
                        "text": {"type": "string", "minLength": 1, "maxLength": 4000},
                        "device_id": {"type": "string"},
                        "contact": {"type": "object"},
                        "confirm": {"type": "boolean"},
                    },
                    "required": ["to", "text"],
                },
            ),
            AdapterAction(
                "phone.call",
                "phone:act",
                "Queue a phone call for a registered iPhone",
                parameters={
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "minLength": 1, "maxLength": 256},
                        "device_id": {"type": "string"},
                        "contact": {"type": "object"},
                        "confirm": {"type": "boolean"},
                    },
                    "required": ["to"],
                },
            ),
            AdapterAction(
                "facetime.call",
                "phone:act",
                "Queue a FaceTime call for a registered iPhone",
                parameters={
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "minLength": 1, "maxLength": 256},
                        "video": {"type": "boolean"},
                        "device_id": {"type": "string"},
                        "contact": {"type": "object"},
                        "confirm": {"type": "boolean"},
                    },
                    "required": ["to"],
                },
            ),
        ),
    ),
    SearchAdapter(
        slug="search",
        name="Web Search",
        description="Permissioned web/research search with a provider interface.",
        capabilities=("search:read",),
        default_scopes=("search:read",),
        min_privacy="normal",
        privacy_kind="app",
        event_types=(),
        actions=(
            AdapterAction(
                "search.query",
                "search:read",
                "Run a permissioned web search and return cited results",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 1, "maxLength": 500},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                    "required": ["query"],
                },
            ),
        ),
    ),
    OctoPrintAdapter(
        slug="octoprint",
        name="OctoPrint",
        description="Owner 3D printer via OctoPrint/Moonraker or a local fake job.",
        capabilities=("printer:read", "printer:act"),
        default_scopes=("printer:read", "printer:act"),
        min_privacy="normal",
        privacy_kind="app",
        actions=(
            AdapterAction("octoprint.ping", "printer:read", "Ping the printer"),
            AdapterAction("octoprint.start", "printer:act", "Start a queued print"),
            AdapterAction("octoprint.status", "printer:read", "Poll a print job"),
        ),
    ),
    CameraAdapter(
        slug="cameras",
        name="Owner cameras",
        description="Replay owner-added cameras only. Never discover the LAN.",
        capabilities=("camera:read",),
        default_scopes=("camera:read",),
        min_privacy="private",
        privacy_kind="app",
        actions=(
            AdapterAction("cameras.clip", "camera:read", "Fetch an owner clip"),
            AdapterAction("cameras.list", "camera:read", "List owner-added cameras"),
        ),
    ),
    DroneAdapter(
        slug="drone",
        name="Owner drone",
        description="Leashed owner drone: takeoff, hover, land, return-to-launch.",
        capabilities=("drone:act",),
        default_scopes=("drone:act",),
        min_privacy="normal",
        privacy_kind="app",
        actions=(
            AdapterAction("drone.takeoff", "drone:act", "Take off (confirm required)"),
            AdapterAction("drone.hover", "drone:act", "Hover in place"),
            AdapterAction("drone.land", "drone:act", "Land"),
            AdapterAction("drone.rtl", "drone:act", "Return to launch"),
        ),
    ),
    PublicFeedsAdapter(
        slug="public_feeds",
        name="Public feeds",
        description="Owner-picked RSS and NWS public alerts. Not a private scanner.",
        capabilities=("feeds:read",),
        default_scopes=("feeds:read",),
        min_privacy="normal",
        privacy_kind="app",
        actions=(
            AdapterAction("public_feeds.poll", "feeds:read", "Poll an owner-picked public feed"),
        ),
    ),
)


class IntegrationRegistry:
    """Adapter registry: built-ins plus config-registered custom adapters."""

    def __init__(self) -> None:
        self._adapters: dict[str, Adapter] = {}
        for adapter in BUILTIN_ADAPTERS:
            self.register(adapter)

    def register(self, adapter: Adapter) -> None:
        if adapter.slug in self._adapters:
            raise ValueError(f"adapter '{adapter.slug}' is already registered")
        self._adapters[adapter.slug] = adapter

    def unregister(self, slug: str) -> None:
        self._adapters.pop(slug, None)

    def get(self, slug: str) -> Adapter | None:
        return self._adapters.get(slug)

    def all(self) -> list[Adapter]:
        return list(self._adapters.values())


registry = IntegrationRegistry()
