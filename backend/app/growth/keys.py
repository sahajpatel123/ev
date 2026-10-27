"""Eval-key routing and suite child environments (GE2).

The eval key lives in the vault-backed key store; suite children get it as
EV_OPENROUTER_API_KEY while every live credential is stripped. Three
re-injection paths were found by review and are all closed here:

1. ``app/config.py:_load_production_secret_overlay`` injects
   ``~/.ev/secrets/production.env`` at import. Closed by pointing
   EV_SECRETS_FILE at a path that cannot exist.
2. ``gauntlet/real_brain.py:_prime_env`` re-reads repo .env + production.env
   and ``load_provider_status`` preferred the FILE key. Fixed there
   (env-first); this module additionally strips the file-sourced names.
3. ``app/config.py:_alias_unprefixed_provider_keys`` copies unprefixed
   vendor names (OPENROUTER_API_KEY) into EV_ names. Closed by stripping
   unprefixed vendor names too.

In-process suites (journeys) keep the ambient env: documented exception,
safe because they make no provider calls (local lane functions only).
"""

from __future__ import annotations

import json
import os
import re
import stat
import urllib.request
from pathlib import Path

# Matches any var that smells like a credential, e.g. EV_MASTER_KEY,
# EV_VAULT_KEY, EV_GOOGLE_API_KEY, SENTRY_DSN, SLACK_WEBHOOK_URL.
_STRIP_RE = re.compile(
    r"(?:^|_)(KEY|SECRET|TOKEN|PASSWORD|PRIVATE|DSN)(?:$|_)|WEBHOOK",
    re.IGNORECASE,
)

# Unprefixed vendor names the app aliases into EV_ names at import.
_UNPREFIXED = ("OPENROUTER_API_KEY", "GOOGLE_API_KEY")

# Where suite children look for production secrets: nowhere.
NO_SECRETS_FILE = "/nonexistent/ge-eval-no-production-secrets"

# Suite children import app.config, whose vault_key requires >= 16 chars.
# They get this fixed dummy (not a secret): enough to boot on scratch
# state, useless for decrypting anything real.
DUMMY_VAULT_KEY = "ge-eval-dummy-vault-key-not-a-secret"

# Vars that may survive the strip because this module sets them deliberately.
_ROUTED = {"EV_OPENROUTER_API_KEY"}


def eval_key_file() -> Path:
    return Path.home() / ".ev" / "secrets" / "eval.env"


def load_eval_key() -> str:
    """Eval key from settings, else ~/.ev/secrets/eval.env. Memory only."""
    try:
        from app.config import settings

        v = str(getattr(settings, "openrouter_eval_key", "") or "").strip()
        if v:
            return v
    except Exception:
        pass
    p = eval_key_file()
    if p.is_file():
        for line in p.read_text().splitlines():
            if line.startswith("EV_OPENROUTER_EVAL_KEY="):
                return line.split("=", 1)[1].strip().strip("\"'")
    return os.environ.get("EV_OPENROUTER_EVAL_KEY", "")


def write_eval_key(value: str, path: Path | None = None) -> Path:
    """Atomically store the eval key, 0600 from creation. Value never logged."""
    p = Path(path) if path else eval_key_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    try:
        os.write(fd, f"EV_OPENROUTER_EVAL_KEY={value.strip()}\n".encode())
    finally:
        os.close(fd)
    tmp.rename(p)
    return p


def _is_live_credential(name: str) -> bool:
    if name in _ROUTED or name == "EV_SECRETS_FILE":
        return False
    if name in _UNPREFIXED:
        return True
    return bool(_STRIP_RE.search(name))


def needs_eval_key(budget: float | None) -> bool:
    """Spend requested means the eval key is required (None/0 run free paths)."""
    return (budget or 0) > 0


def suite_env(*, eval_key: str | None = None, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a child env with the eval key routed and live creds stripped.

    ``extra`` is applied BEFORE the strip so callers cannot reintroduce a
    live credential; the routed eval key is set last as the one exception.
    """
    if eval_key is None:
        eval_key = load_eval_key()
    env = dict(os.environ)
    if extra:
        env.update(extra)
    for name in [n for n in env if _is_live_credential(n)]:
        env.pop(name, None)
    if eval_key:
        env["EV_OPENROUTER_API_KEY"] = eval_key
    env["EV_GROWTH_EVAL"] = "1"
    env["EV_SECRETS_FILE"] = NO_SECRETS_FILE
    env["EV_VAULT_KEY"] = DUMMY_VAULT_KEY
    # Eval runs must not pollute the live audit log (GE4 mines it for
    # REAL failures): lane audits in suite children go to scratch.
    if "EV_FABRIC_AUDIT_FILE" not in env:
        import tempfile
        env["EV_FABRIC_AUDIT_FILE"] = str(
            Path(tempfile.mkdtemp(prefix="ge-audit-")) / "audit.jsonl")
    return env


def key_credit(key: str, *, timeout: float = 20.0) -> dict:
    """OpenRouter key usage/limit numbers. Key value never leaves this call."""
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/auth/key",
        headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    d = data.get("data", data) or {}
    try:
        usage = float(d.get("usage") or 0.0)
    except (TypeError, ValueError):
        usage = 0.0
    limit = d.get("limit")
    try:
        limit = float(limit) if limit is not None else None
    except (TypeError, ValueError):
        limit = None
    return {"label": d.get("label"), "usage": usage, "limit": limit,
            "remaining": (limit - usage) if limit is not None else None,
            "is_free_tier": d.get("is_free_tier")}
