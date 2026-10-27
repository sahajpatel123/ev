"""Eval/live key separation: suites can never consume the live key.

Mechanism: the growth runner builds child envs with the live key REMOVED
and EV_OPENROUTER_API_KEY set to the eval key, so existing readers work
unmodified while the live value is absent from suite processes. Proven by
test (live value absent from suite_env()).
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

LIVE_KEY_VARS = ("EV_OPENROUTER_API_KEY", "EV_MASTER_KEY", "EV_VAULT_KEY",
                 "EV_API_KEY", "EV_GOOGLE_API_KEY")


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
    return ""


def write_eval_key(value: str, path: Path | None = None) -> Path:
    """Atomically store the eval key, chmod 600. Value never logged."""
    p = Path(path) if path else eval_key_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(f"EV_OPENROUTER_EVAL_KEY={value.strip()}\n")
    os.chmod(tmp, 0o600)
    tmp.rename(p)
    os.chmod(p, 0o600)
    return p


def suite_env(extra: dict | None = None) -> dict:
    """Child env for suite subprocesses: live keys absent, eval key routed in."""
    env = dict(os.environ)
    for var in LIVE_KEY_VARS:
        env.pop(var, None)
    ev = load_eval_key()
    if ev:
        env["EV_OPENROUTER_API_KEY"] = ev
    env["EV_GROWTH_EVAL"] = "1"
    if extra:
        env.update(extra)
    return env


def needs_eval_key(budget: float | None) -> bool:
    return (budget or 0) > 0


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
