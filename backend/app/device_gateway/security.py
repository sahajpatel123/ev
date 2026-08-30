"""Threat model and origin policy for the Device Gateway / PWA.

Stolen phone / copied IndexedDB device token: revoke device; access tokens expire;
WS tickets are one-time. Residual: attacker can act until revoke.

XSS: CSP + no innerHTML of model text. Residual: a CSP bypass could read IndexedDB.

Replay: pairing one-time; WS tickets one-time; Mac actions idempotent. Residual:
duplicate harmless canaries.

Rogue origin: allowlist loopback + *.ts.net; gateway middleware overwrites CORS *.

Pairing interception: short TTL + HTTPS. Residual: tailnet member who intercepts
HTTPS (shouldn't) could pair; still sandboxed.

Revoked device: HTTP 401, live socket closed, tools denied.

Session fixation: tickets bound to session_id.

Stale service worker: protocol/build mismatch forces reload.

Public exposure: loopback bind; Funnel must stay off; Serve is tailnet-only.

Compromised tailnet member: still needs Evie pairing. Residual: they can reach
the PWA login screen.

Duplicated tab: instance_id + lease.

Do not trust Tailscale identity headers unless the peer is localhost (Serve).
"""

from __future__ import annotations

ALLOWED_ORIGIN_SUFFIXES = (".ts.net",)
ALLOWED_ORIGIN_HOSTS = {"127.0.0.1", "localhost", "::1"}
TAILSCALE_IDENTITY_HEADERS = (
    "tailscale-user-login",
    "tailscale-user-name",
    "x-forwarded-for",
)


def origin_allowed(origin: str | None, host: str | None) -> bool:
    if not origin:
        # Non-browser clients (tests, curl, native). Browsers always send Origin
        # on credentialed CORS/fetch from a page.
        return True
    value = origin.strip().lower()
    if value in {"null", "undefined"}:
        return False
    host_part = value.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    if host_part in ALLOWED_ORIGIN_HOSTS:
        return True
    if host_part.endswith(ALLOWED_ORIGIN_SUFFIXES):
        return True
    if host:
        only = host.split(":", 1)[0].lower()
        if only in ALLOWED_ORIGIN_HOSTS or only.endswith(ALLOWED_ORIGIN_SUFFIXES):
            return host_part == only
    return False


def request_from_serve_localhost(client_host: str | None) -> bool:
    return (client_host or "") in {"127.0.0.1", "::1"}


def tailscale_identity(headers: dict[str, str], *, client_host: str | None) -> dict[str, str] | None:
    if not request_from_serve_localhost(client_host):
        return None
    login = headers.get("tailscale-user-login") or headers.get("Tailscale-User-Login")
    if not login:
        return None
    return {
        "login": login,
        "name": headers.get("tailscale-user-name") or headers.get("Tailscale-User-Name") or "",
    }
