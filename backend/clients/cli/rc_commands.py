"""RC (release captain) CLI commands: thin honest readers over existing engines.

- ``ev cost``: model-call spend from the ledger (gateway/costs.py).
- ``ev privacy ledger``: recent egress rows with data class + tier.
- ``ev trace``: finished agent-run traces (~/.ev/agent/runs).
- ``ev permissions``: prompt-free permission probes (fabric probes).
- ``ev hands``: HANDS owner checks (skills/check.py passthrough).
- ``ev keys``: which secrets are configured and where (never values).
- ``ev accept``: guided owner-acceptance checklist (resumable; records PASS/FAIL).

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


# -- accept -------------------------------------------------------------------

_ACCEPT_ITEMS: tuple[tuple[str, str, str, str], ...] = (
    ("permissions", "auto", "Permissions green",
     "Runs the prompt-free permission probes (Accessibility, Screen Recording, Automation, browsers, Ghostty, OCR helper, CLIs)."),
    ("keychain", "guided", "Keychain prompt seen",
     "Approve the macOS Keychain prompt for Evie (device token com.ev.suit.*). Confirm you saw and approved it."),
    ("gesture", "guided", "Gesture calibration: 60s typing, zero false triggers",
     "Hold Option to talk, double-tap to latch, Esc to stop. Then type normally for 60 seconds: the orb must never activate. Confirm zero false triggers."),
    ("voice", "guided", "Voice round trip + barge-in",
     "Hold Option, ask something, hear the answer. Mid-answer say 'stop' (or interrupt): playback must stop promptly and no orphan effect may run."),
    ("orb", "guided", "Orb visible and calm on your displays",
     "Check the notch island orb on each display: visible, calm when idle, animates while listening/speaking. Confirm Reduce Motion is respected if you use it."),
    ("t1dry", "auto", "T1 plan from your Ghostty screenshot (dry run)",
     "Screenshot your Ghostty window, then this prints the inferred split layout + per-pane resume plan. Nothing is opened."),
    ("t1apply", "guided", "T1 apply: new tab matches, sessions resume",
     "After the dry plan looks right: apply it, confirm the new tab matches the layout and sessions resume, then close that tab."),
    ("t2", "guided", "T2 skip on a real YouTube ad",
     "Play a YouTube video with an ad (Chrome: View > Developer > Allow JavaScript from Apple Events). Say 'skip the ad': it must skip within ~1.5s, or answer honestly if unskippable."),
    ("iphone", "guided", "iPhone: PWA, approval from phone, cellular",
     "Add to Home Screen; send an approval from the phone; run one turn over cellular via Tailscale. Confirm each works."),
    ("send", "guided-optional", "Guided live send through the confirm path (optional)",
     "Dictate a real message to someone safe: confirm card must appear, spoken 'yes' must NOT send, physical approve sends, receipt verifies."),
    ("bgreport", "guided", "Background task reports back to the phone",
     "Start a long task, switch to the phone: the result must arrive on the same thread when done."),
)


def _accept_state_path() -> Path:
    override = os.environ.get("ACCEPT_STATE_PATH")
    if override:
        return Path(override)
    return Path.home() / ".ev" / "accept-state.json"


def _accept_report_path() -> Path:
    override = os.environ.get("ACCEPT_REPORT_PATH")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "docs" / "OWNER_ACCEPTANCE.md"


def _accept_load() -> dict:
    import json as _json

    p = _accept_state_path()
    if p.is_file():
        try:
            data = _json.loads(p.read_text())
            if isinstance(data, dict):
                return data
        except ValueError:
            pass
    return {"results": {}}


def _accept_save(state: dict) -> None:
    import json as _json
    from datetime import datetime, timezone

    state["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    p = _accept_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(_json.dumps(state, indent=1))
    tmp.rename(p)


def _accept_record(state: dict, item_id: str, status: str, note: str = "") -> None:
    import datetime as _dt

    state.setdefault("results", {})[item_id] = {
        "status": status,
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "note": note[:300],
    }
    _accept_save(state)


def _accept_write_report(state: dict) -> Path:
    results = state.get("results", {})
    done = sum(1 for _id, _k, _t, _d in _ACCEPT_ITEMS if results.get(_id, {}).get("status") == "PASS")
    opt = sum(1 for _id, k, _t, _d in _ACCEPT_ITEMS if k == "guided-optional")
    lines = ["# Owner acceptance (Evie v1.0)", "",
             f"Updated: {state.get('updated_at', '?')} — {done}/{len(_ACCEPT_ITEMS) - opt} required PASS.",
             "Recorded by `ev accept` (resumable). FAIL/SKIP notes feed `ev improve`.", "",
             "| Item | Kind | Status | When (UTC) | Note |",
             "| --- | --- | --- | --- | --- |"]
    for _id, kind, title, _desc in _ACCEPT_ITEMS:
        r = results.get(_id, {})
        lines.append(f"| {title} (`{_id}`) | {kind} | {r.get('status', 'TODO')} | {r.get('ts', '')} | {r.get('note', '')} |")
    out = _accept_report_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n")
    return out


def _accept_auto(item_id: str) -> tuple[bool, str]:
    if item_id == "permissions":
        from app.fabric.skills.check import main as hands_main

        rc = int(hands_main(["permissions"]))
        return rc == 0, "hands check permissions rc=%d" % rc
    if item_id == "t1dry":
        from app.fabric.skills.check import main as hands_main

        rc = int(hands_main(["t1"]))
        return rc == 0, "hands check t1 rc=%d (dry run, nothing opened)" % rc
    return False, "unknown auto item"


def cmd_accept(argv: list[str]) -> int:
    args = list(argv or [])
    state = _accept_load()
    if "--list" in args or "list" in args:
        results = state.get("results", {})
        for _id, kind, title, _desc in _ACCEPT_ITEMS:
            print(f"  [{results.get(_id, {}).get('status', 'TODO'):4s}] {_id:12s} ({kind}) {title}")
        return 0
    if "--report" in args:
        out = _accept_write_report(state)
        print(f"wrote {out}")
        return 0
    redo = [a.split("=", 1)[1] for a in args if a.startswith("--redo=")]
    if redo:
        state.get("results", {}).pop(redo[0], None)
        _accept_save(state)
        print(f"cleared {redo[0]}")
        return 0
    pending = [it for it in _ACCEPT_ITEMS if state.get("results", {}).get(it[0], {}).get("status") not in ("PASS", "FAIL", "SKIP")]
    if not pending:
        out = _accept_write_report(state)
        print(f"all items recorded. report: {out}")
        return 0
    _id, kind, title, desc = pending[0]
    print(f"next: {title} (`{_id}`, {kind})\n  {desc}")
    if kind == "auto":
        ok, note = _accept_auto(_id)
        _accept_record(state, _id, "PASS" if ok else "FAIL", note)
        print(f"  -> {'PASS' if ok else 'FAIL'}: {note}")
    else:
        ans = input("  did it pass? [y]es / [n]o / [s]kip (+ optional note after a space): ").strip()
        if not ans:
            print("  left TODO.")
            return 0
        verdict = {"y": "PASS", "n": "FAIL", "s": "SKIP"}.get(ans[0].lower())
        if verdict is None:
            print("  left TODO.")
            return 0
        note = ans[1:].strip()
        _accept_record(state, _id, verdict, note)
        print(f"  recorded {verdict}.")
    _accept_write_report(state)
    remaining = [it for it in _ACCEPT_ITEMS if state.get("results", {}).get(it[0], {}).get("status") not in ("PASS", "FAIL", "SKIP")]
    print(f"  remaining: {len(remaining)}. re-run `ev accept` to continue.")
    return 0
