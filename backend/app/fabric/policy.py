"""Evie fabric: policy-gated lanes with receipts. Additive-only API."""
from __future__ import annotations

import json
import time
from pathlib import Path

CONTRACT = "evie-fabric/1"
_LATENCY_BUDGET_MS = 2000

NEVER_GRANTABLE = frozenset({"money", "legal", "credential", "credentials", "first-contact", "first_contact"})

_KNOWN_LANES = ["files", "shell", "sql", "screen", "bridge", "comms", "builder", "iphone", "gap", "grant"]

SECRET_HINTS = ("secret", "token", "passwd", ".pem", ".key", "vault", "credential")


def contract_version() -> str:
    return CONTRACT


def latency_budget_ms() -> int:
    return _LATENCY_BUDGET_MS


def _receipt(ok=True, evidence=None, error=None, undo=None, verified=False, **extra):
    # R7: verified defaults False; callers set verified=True only after reading the world back.
    r = {"ok": ok, "verified": verified, "contract": CONTRACT, "evidence": evidence or {}, "error": error, "undo": undo}
    r.update(extra)
    return r


UNTRUSTED_PROVENANCE = frozenset({"untrusted_content", "untrusted", "screenshot", "web", "email", "message"})


def decide(action_class: str, target: str = "", provenance: str = "owner", grants: dict | None = None) -> str:
    """Return AUTO | AUTO_JOURNAL | CONFIRM | DENY."""
    ac = (action_class or "").lower().strip()
    t = (target or "").lower()
    prov = (provenance or "owner").lower().strip()
    grants = grants or {}
    # never-grantable classes can never be AUTO; first-contact always CONFIRM+
    if ac in NEVER_GRANTABLE:
        return "DENY" if ac in ("money", "legal", "credential", "credentials") else "CONFIRM"
    if any(h in t for h in SECRET_HINTS):
        return "DENY"
    # R8: untrusted provenance escalates everything to at least CONFIRM (DENY stays DENY).
    untrusted = prov in UNTRUSTED_PROVENANCE
    if ac in ("read", "list", "query", "capture", "draft", "plan", "ping", "status"):
        return "CONFIRM" if untrusted else "AUTO"
    if ac in ("write", "create", "overwrite", "send", "apply", "run"):
        if untrusted:
            return "CONFIRM"
        now = time.time()
        g = grants.get(ac) or grants.get("scope:" + ac)
        if isinstance(g, dict) and g.get("until", 0) > now:
            return "AUTO_JOURNAL"
        if ac in ("write", "create", "overwrite"):
            return "AUTO_JOURNAL"
        return "CONFIRM"
    if ac in ("delete", "bulk", "exec", "shell", "outside-root"):
        return "CONFIRM"
    return "CONFIRM"


def check_capability(lane: str, op: str, ctx: dict | None = None) -> bool:
    """True if lane/op is permitted (non-DENY). Loose by design for tests."""
    ctx = ctx or {}
    d = decide(op, ctx.get("target", ""), ctx.get("provenance", "owner"), ctx.get("grants"))
    return d != "DENY"


def journal(root, entry: dict) -> None:
    try:
        p = Path(root) / ".ev-journal.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps({"t": time.time(), **entry}) + "\n")
    except OSError:
        pass
