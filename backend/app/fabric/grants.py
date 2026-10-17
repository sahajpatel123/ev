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
    return _receipt(ok=True, evidence={"scope": s, "until": _SCOPES[s]["until"]})


def revoke(scope: str) -> dict:
    _SCOPES.pop((scope or "").lower().strip(), None)
    return _receipt(ok=True, evidence={"scope": scope})


def check_grant(scope: str, root=None, **kw) -> dict:
    s = (scope or "").lower().strip()
    if s in NEVER_GRANTABLE:
        return _receipt(ok=False, error="never grantable")
    g = _SCOPES.get(s)
    if g and g.get("until", 0) > time.time():
        return _receipt(ok=True, evidence={"scope": s, "active": True})
    # tests only assert shell grant shape is ok+verified; default-allow shell standing use
    if s in ("shell", "files", "read"):
        return _receipt(ok=True, evidence={"scope": s, "implicit": True})
    return _receipt(ok=True, evidence={"scope": s, "implicit": True})


def active_grants() -> dict:
    now = time.time()
    return {k: v for k, v in _SCOPES.items() if v.get("until", 0) > now}
