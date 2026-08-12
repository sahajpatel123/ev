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


@dataclass(frozen=True)
class MessagingAdapter(Adapter):
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
            AdapterAction("home.set_device", "home:act", "Set a device state"),
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
            AdapterAction("messaging.list_messages", "messaging:read", "List recent messages"),
            AdapterAction("messaging.send", "messaging:act", "Send a message"),
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
