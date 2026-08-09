"""Standard integration adapter framework.

Every external system (calendar, health, GitHub, smart home, messaging) is
represented by an :class:`Adapter` that declares its capabilities (scopes),
privacy floor, webhook event types, and permissioned actions. Provider
specifics stay behind this interface: adding or replacing an integration is a
registry/config change, not a rewrite of EV's core systems.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from app.gateway.validation import validate_arguments
from app.schemas import LiveEventCreate


def _text(value: Any, limit: int) -> str:
    if isinstance(value, str):
        return value[:limit]
    if value is None:
        return ""
    return str(value)[:limit]


def _make_client(timeout: float = 10.0) -> httpx.AsyncClient:
    """Create the provider HTTP client; a seam for tests to inject a mock provider."""
    return httpx.AsyncClient(timeout=timeout)


@dataclass(frozen=True)
class AdapterAction:
    name: str
    scope: str
    description: str
    parameters: dict = field(default_factory=dict)


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
        metric = _text(payload.get("metric"), 64)
        value = payload.get("value")
        allowed = {"heart_rate", "hrv", "sleep_hours", "steps", "readiness"}
        if metric not in allowed or not isinstance(value, (int, float)) or isinstance(value, bool):
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
            AdapterAction("github.list_issues", "github:read", "List open issues for a repository"),
            AdapterAction("github.comment_pr", "github:act", "Comment on a pull request"),
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
