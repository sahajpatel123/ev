"""Standing grants with expiry. Money/legal/credential/first-contact never grantable."""
from __future__ import annotations

import time

from .policy import NEVER_GRANTABLE, _receipt

_SCOPES: dict[str, dict] = {}


def grant(scope: str, ttl_s: float = 3600) -> dict:
    s = (scope or "").lower().strip()
    if s in NEVER_GRANTABLE or not s:
        return _receipt(ok=False, error=f"scope '{scope}' is never grantable")
    _SCOPES[s] = {"scope": s, "until": time.time() + ttl_s}
    ok = s in _SCOPES and _SCOPES[s]["until"] > time.time()
    return _receipt(ok=ok, verified=ok, evidence={"scope": s, "until": _SCOPES[s]["until"]})


def revoke(scope: str) -> dict:
    _SCOPES.pop((scope or "").lower().strip(), None)
    gone = (scope or "").lower().strip() not in _SCOPES
    return _receipt(ok=gone, verified=gone, evidence={"scope": scope})


def check_grant(scope: str, root=None, **kw) -> dict:
    s = (scope or "").lower().strip()
    if s in NEVER_GRANTABLE:
        return _receipt(ok=False, error="never grantable")
    g = _SCOPES.get(s)
    if g and g.get("until", 0) > time.time():
        return _receipt(ok=True, verified=True, evidence={"scope": s, "active": True})
    # D9: standing grants are OFF by default — no grant means not granted.
    return _receipt(ok=False, verified=True, evidence={"scope": s, "active": False})


def active_grants() -> dict:
    now = time.time()
    return {k: v for k, v in _SCOPES.items() if v.get("until", 0) > now}


def revoke_all() -> dict:
    _SCOPES.clear()
    return _receipt(ok=True, verified=True, evidence={"revoked_all": True})
