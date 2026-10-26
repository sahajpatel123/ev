"""RC (release captain) CLI commands: thin honest readers over existing engines.

- ``ev cost``: model-call spend from the ledger (gateway/costs.py).
- ``ev privacy ledger``: recent egress rows with data class + tier.
- ``ev trace``: finished agent-run traces (~/.ev/agent/runs).
- ``ev permissions``: prompt-free permission probes (fabric probes).
- ``ev hands``: HANDS owner checks (skills/check.py passthrough).
- ``ev keys``: which secrets are configured and where (never values).

Local-only: no API key required, nothing leaves the machine.
"""

from __future__ import annotations

import os
from pathlib import Path

# -- cost ---------------------------------------------------------------

def _no_ledger(exc: Exception) -> bool:
    message = f"{type(exc).__name__}: {exc}".lower()
    return "no such table" in message or "does not exist" in message or "undefinedtable" in message


async def cmd_cost(window_hours: int = 24) -> int:
    from app.config import settings as _settings
    from app.db import SessionLocal
    from app.gateway import costs as costs_mod
    from app.services.model_call import model_call_stats

    try:
        async with SessionLocal() as session:
            stats = await model_call_stats(session, window_hours=window_hours)
            month = await costs_mod.monthly_cost_usd(session)
    except Exception as exc:
        if _no_ledger(exc):
            print("cost ledger: no model calls recorded yet (empty database)")
            return 0
        raise
    totals = stats.get("totals", {})
    print(f"Evie cost — last {window_hours}h + month to date (provider-reported where available)")
    print(f"  calls: {totals.get('calls', 0)}  errors: {totals.get('errors', 0)}  "
          f"blocked: {totals.get('blocked', 0)}")
    print(f"  tokens: {totals.get('prompt_tokens', 0)} prompt / "
          f"{totals.get('completion_tokens', 0)} completion")
    print(f"  latency: avg {totals.get('avg_latency_ms', 0)} ms, "
          f"p95 {totals.get('p95_latency_ms', 0)} ms")
    for row in stats.get("by_provider_model", []):
        print(f"  {row.get('provider')}/{row.get('model')}: "
              f"{row.get('calls', 0)} calls, "
              f"{row.get('prompt_tokens', 0)}+{row.get('completion_tokens', 0)} tokens")
    print(f"  month spend: ${month:.4f}")
    if _settings.cost_cap_enabled:
        print(f"  cap: enforced at ${_settings.monthly_cost_cap_usd:.2f}/month "
              f"({'EXCEEDED' if month > _settings.monthly_cost_cap_usd else 'ok'})")
    else:
        print("  cap: disabled (EV_COST_CAP_ENABLED=false)")
    return 0


# -- privacy ------------------------------------------------------------

async def cmd_privacy_ledger(limit: int = 20) -> int:
    from app.db import SessionLocal
    from app.services.model_call import list_model_calls

    try:
        async with SessionLocal() as session:
            rows = await list_model_calls(session, limit=limit)
    except Exception as exc:
        if _no_ledger(exc):
            print("egress ledger: no model calls recorded yet (empty database)")
            return 0
        raise
    if not rows:
        print("egress ledger: no model calls recorded "
              "(logging needs model_call_log_enabled + at least one call)")
        return 0
    print(f"egress ledger — {len(rows)} most recent remote calls (data class + tier):")
    for row in rows:
        meta = (row.envelope or {}).get("metadata") or {}
        data_class = meta.get("data_class") or "?"
        tier = meta.get("reasoning_effort") or meta.get("tier") or "?"
        cost = meta.get("cost_usd")
        cost_s = f"${cost:.6f}" if isinstance(cost, (int, float)) else "?"
        print(f"  {row.created_at} {row.provider}/{row.model or '?'} "
              f"class={data_class} tier={tier} "
              f"tok={row.prompt_tokens or 0}+{row.completion_tokens or 0} "
              f"cost={cost_s} status={row.status}")
    return 0


# -- trace --------------------------------------------------------------

def cmd_trace_list(limit: int = 20) -> int:
    from app.agent import store as trace_store

    runs = trace_store.list_runs(limit=limit)
    if not runs:
        print(f"no finished runs in {trace_store.trace_dir()}")
        return 0
    print(f"agent traces in {trace_store.trace_dir()}:")
    for run in runs:
        print(f"  {run.get('run_id')} [{run.get('status')}] "
              f"{str(run.get('goal') or '')[:80]} "
              f"({run.get('tool_calls', 0)} calls, {run.get('elapsed_ms', 0)} ms)")
    return 0


def cmd_trace_show(run_id: str) -> int:
    import json as _json

    from app.agent import store as trace_store

    run = trace_store.load_run(run_id)
    if run is None:
        print(f"no trace for run {run_id!r} in {trace_store.trace_dir()}")
        return 1
    print(f"run {run.get('run_id')} [{run.get('status')}]")
    print(f"  goal: {run.get('goal')}")
    print(f"  summary: {run.get('summary') or run.get('error') or ''}")
    print(f"  calls: brain={run.get('brain_calls', '?')} tools={run.get('tool_calls', 0)} "
          f"messages={run.get('messages', 0)}")
    for step in run.get("steps", [])[:30]:
        mark = "ok" if step.get("ok") else "FAIL"
        print(f"    {mark} {step.get('tool')}: "
              f"{str(step.get('error') or step.get('summary') or '')[:100]}")
    usage = run.get("usage")
    if usage:
        print(f"  usage: {_json.dumps(usage, default=str)[:300]}")
    return 0


# -- permissions ----------------------------------------------------------

def cmd_permissions() -> int:
    from app.fabric import probes as probes_mod

    rows = probes_mod.permission_entries()
    bad = 0
    for entry in rows:
        status = str(entry.get("status") or "unknown")
        mark = {"granted": "PASS", "denied": "FAIL"}.get(status, "SKIP")
        if mark == "FAIL":
            bad += 1
        fix = f" | fix: {entry['fix']}" if entry.get("fix") else ""
        print(f"  [{mark}] {entry.get('permission')} [{entry.get('kind')}] — "
              f"{entry.get('detail')}{fix}")
    return 1 if bad else 0


# -- hands ------------------------------------------------------------------

def cmd_hands(argv: list[str]) -> int:
    from app.fabric.skills.check import main as hands_main

    return int(hands_main(argv))


# -- keys -------------------------------------------------------------------

_KEY_SOURCES: tuple[tuple[str, str, str], ...] = (
    ("EV_MASTER_KEY", "env", "API + CLI master credential"),
    ("EV_VAULT_KEY", "env", "vault encryption key (never derived from master)"),
    ("EV_API_KEY", "env", "CLI/API bearer (usually the master key)"),
    ("EV_OPENROUTER_API_KEY", "env", "brain provider (OpenRouter)"),
    ("EV_GOOGLE_API_KEY", "env", "voice provider (Google AI API)"),
)


def cmd_keys() -> int:
    print("keys — presence and source only (values are never printed):")
    search: list[Path] = []
    secrets_dir = Path.home() / ".ev" / "secrets"
    if secrets_dir.is_dir():
        search += sorted(secrets_dir.glob("*.env")) + sorted(secrets_dir.glob("*.key"))
    for local in (Path.cwd() / ".env", Path.cwd() / "backend" / ".env"):
        if local.is_file():
            search.append(local)
    for name, _source, purpose in _KEY_SOURCES:
        if os.environ.get(name, ""):
            print(f"  [SET] {name} via environment — {purpose}")
            continue
        file_hit = ""
        for candidate in search:
            try:
                text = candidate.read_text(errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                stripped = line.strip()
                if not stripped.startswith(f"{name}="):
                    continue
                value = stripped[len(name) + 1:].strip().strip("\"'")
                if value and not value.startswith("#"):
                    file_hit = str(candidate)
                    break
            if file_hit:
                break
        if file_hit:
            print(f"  [SET] {name} via {file_hit} — {purpose}")
        else:
            print(f"  [UNSET] {name} — {purpose}")
    print("  device tokens live in the macOS Keychain (com.ev.suit.*), managed by Evie.app")
    print("  (server keys load from the service environment / repo .env at launch)")
    return 0
