"""Evie fabric: policy-gated lanes with receipts. Additive-only API."""
from __future__ import annotations

import json
import re
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


# --------------------------------------------------------------------------- #
# Risk floor (R7 / D4)
#
# A small set of effects is never lowered to AUTO by the owner's autonomy
# setting: sends, calls, SQL writes, permanent deletes, publishing, data
# egress, money and credentials. Each one asks, an approval covers only the
# call that was approved, and the approval must be physical (HUD card, paired
# phone, typed reply). A spoken "yes" never approves one of these.
# --------------------------------------------------------------------------- #

HIGH_RISK = frozenset({
    "send", "call", "sql_write", "delete", "publish", "egress",
    "money", "credential", "first_contact",
})

_SEND_OPS = frozenset({"send", "reply", "forward", "reply_all", "message"})
_DELETE_OPS = frozenset({"delete", "trash", "remove", "purge", "empty_trash", "destroy"})
_MONEY_WORDS = ("pay", "purchase", "buy", "checkout", "transfer", "venmo", "payment")

_SHELL_DELETE = re.compile(
    r"(?:^|[;&|(]\s*|\s)(?:rm|rmdir|shred|srm|unlink)\s"
    r"|git\s+(?:reset\s+--hard|clean\s+-[a-z]*f|checkout\s+--\s|branch\s+-D|stash\s+(?:drop|clear))"
    r"|\bdd\s+if="
    r"|\b(?:kill|killall|pkill)\s"
    r"|\bdropdb\b|\bdiskutil\s+erase"
    r"|>\s*/dev/(?:disk|rdisk)"
)
_SHELL_PUBLISH = re.compile(
    r"git\s+push\b|\bnpm\s+publish\b|\btwine\s+upload\b|\bgh\s+(?:pr\s+(?:create|merge)|release\s+create)\b"
)
_SHELL_EGRESS = re.compile(
    r"\bcurl\b[^|;]*(?:-X\s*(?:POST|PUT|PATCH|DELETE)|--data\b|-d\s|-F\s|--upload-file|-T\s)"
    r"|\bwget\b[^|;]*--post"
    r"|\b(?:scp|sftp|rsync)\b[^|;]*\S+:"
    r"|\bssh\s"
    r"|\b(?:mail|sendmail|mutt)\s"
    r"|\bnc\s+-?\w*\s*\S+\s+\d+"
)
_SCRIPT_SEND = re.compile(
    r"\bsend\b|make\s+new\s+outgoing\s+message|\bpost\s+message\b|\.send\(", re.I
)
_SCRIPT_DELETE = re.compile(
    r"\bdelete\b|empty\s+(?:the\s+)?trash|do\s+shell\s+script\s+\"?rm\s", re.I
)

_SHELL_ARGS = ("cmd", "command")


def _sql_writes(sql: str) -> bool:
    try:
        from app.fabric.sql_lane import classify

        return classify(sql or "") != "read"
    except Exception:  # noqa: BLE001 - an unparseable statement is treated as a write
        return True


def _shell_risk(cmd: str) -> str | None:
    text = f" {cmd or ''}"
    if _SHELL_PUBLISH.search(text):
        return "publish"
    if _SHELL_EGRESS.search(text):
        return "egress"
    if _SHELL_DELETE.search(text):
        return "delete"
    return None


def risk_class(tool_name: str, permission: str = "", arguments: dict | None = None) -> str | None:
    """The high-risk class of one call, or None for ordinary work.

    The answer is read from what the call does (its permission and its own
    operation argument), never from who asked or how often it was approved.
    """
    name = (tool_name or "").strip()
    perm = (permission or "").lower()
    args = arguments or {}
    op = str(args.get("op") or args.get("operation") or "").strip().lower()

    if perm.endswith(":send") or name == "life.send":
        return "send"
    if name == "phone.call" or perm == "phone:act":
        return "send" if op == "message" else "call"
    if name in {"comms.act", "digital.act"}:
        if op in _SEND_OPS:
            return "send"
        if op in _DELETE_OPS:
            return "delete"
        if any(word in op for word in _MONEY_WORDS):
            return "money"
        return None
    if name in {"sql_query", "sql.query"} or perm in {"sql:write", "fabric:sql"}:
        return "sql_write" if _sql_writes(str(args.get("sql") or "")) else None
    if name == "pim.act":
        return "delete" if op.endswith("_delete") or op in _DELETE_OPS else None
    if name in {"run_applescript", "mac.act"}:
        script = str(args.get("script") or "")
        if _SCRIPT_SEND.search(script):
            return "send"
        if _SCRIPT_DELETE.search(script):
            return "delete"
        return None
    for key in _SHELL_ARGS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return _shell_risk(value)
    return None


def physical_confirm_required(cls: str | None) -> bool:
    """True when only a physical confirmation may approve this class."""
    return bool(cls) and cls in HIGH_RISK


def grant_lifts(cls: str | None) -> bool:
    """A live standing grant lifts the floor, except for never-grantable classes."""
    if not cls or cls in NEVER_GRANTABLE or cls in {"money", "credential", "first_contact"}:
        return False
    try:
        from app.fabric.grants import check_grant

        return bool(check_grant(cls).get("ok"))
    except Exception:  # noqa: BLE001 - no grant store means no grant
        return False


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
