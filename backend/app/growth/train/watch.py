"""GE3 stage 6 WATCH: 24h post-ship monitoring with automatic rollback.

Checks (each honest about its source):
- crash loop: ev.api + ev.talk health reachable and reporting the release
  SHA (same endpoints the deploy wrapper gates on).
- error-rate spike: audit-log ok=false ratio over the trailing hour vs the
  pre-ship hour (durable, from ~/.ev/fabric_audit.jsonl).
- latency p95 / silent turns / false success: PENDING GE5 durable
  telemetry — watch reports them as UNMONITORED, never faked. GE5's
  store plugs in here (see ``GE5_HOOKS``).

On breach: automatic rollback (revert + push + wrapper redeploy + app
restore via ship.rollback, DB never auto-restored) + owner notification
(HUD/phone + OWNER-ACTION file). `ev release watch --check` is the
cron-able entry; expiry after 24h completes the watch with a summary.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

WATCH_FILE = Path.home() / ".ev" / "growth" / "watch.json"
WINDOW_H = 24
ERROR_SPIKE_DELTA = 0.05  # breach if failure ratio rises >= 5 points vs pre-ship

# GE5 plugs durable telemetry in here; until then these stay unmonitored.
GE5_HOOKS = ("latency_p95", "silent_turns", "false_success")


def _health_sha(url: str) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=6) as resp:
            return json.load(resp).get("git", {}).get("sha", "")[:10] or None
    except Exception:
        return None


def audit_fail_ratio(since_h: float, audit_path: Path | None = None) -> tuple[float, int]:
    """(fail_ratio, n) for audit entries in the trailing window. Pure-ish."""
    p = audit_path or (Path.home() / ".ev" / "fabric_audit.jsonl")
    if not p.is_file():
        return 0.0, 0
    cutoff = datetime.now(UTC).timestamp() - since_h * 3600
    n = fails = 0
    for line in p.read_text().splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if float(e.get("t") or 0) < cutoff:
            continue
        n += 1
        if not e.get("ok"):
            fails += 1
    return (fails / n) if n else 0.0, n


def enter_watch(tag: str, prev_sha: str, new_sha: str) -> dict:
    base_ratio, base_n = audit_fail_ratio(1.0)
    state = {"tag": tag, "prev_sha": prev_sha, "new_sha": new_sha,
             "since": datetime.now(UTC).isoformat(), "window_h": WINDOW_H,
             "base_fail_ratio": base_ratio, "base_n": base_n,
             "checks": [], "status": "watching"}
    WATCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    WATCH_FILE.write_text(json.dumps(state, indent=1))
    return state


def load_watch() -> dict | None:
    if not WATCH_FILE.is_file():
        return None
    try:
        return json.loads(WATCH_FILE.read_text())
    except ValueError:
        return None


def check(state: dict) -> dict:
    """One watch check. Returns {breaches, notes, expired}."""
    breaches, notes = [], []
    sha = state["new_sha"][:10]
    api = _health_sha("http://127.0.0.1:8000/v1/health")
    talk = _health_sha("http://127.0.0.1:18000/v1/health")
    if api != sha or talk != sha:
        breaches.append(f"crash/sha-drift: api={api} talk={talk} want={sha}")
    else:
        notes.append(f"health OK api+talk @ {sha}")
    ratio, n = audit_fail_ratio(1.0)
    base, base_n = state.get("base_fail_ratio", 0.0), state.get("base_n", 0)
    if n >= 10 and base_n >= 10 and ratio - base >= ERROR_SPIKE_DELTA:
        breaches.append(f"error-rate spike: {ratio:.1%} (n={n}) vs pre-ship {base:.1%} (n={base_n})")
    else:
        notes.append(f"audit errors {ratio:.1%} (n={n}) vs pre-ship {base:.1%} (n={base_n})")
    for hook in GE5_HOOKS:
        notes.append(f"{hook}: UNMONITORED (pending GE5 durable telemetry)")
    since = datetime.fromisoformat(state["since"])
    expired = datetime.now(UTC) - since > timedelta(hours=state.get("window_h", WINDOW_H))
    return {"breaches": breaches, "notes": notes, "expired": expired}


def _notify_owner(text: str) -> None:
    try:
        import asyncio

        from clients.cli import notify_send
        asyncio.run(notify_send("Evie release watch", text, priority=1.0, sticky=True))
    except Exception as e:  # noqa: BLE001 - notification is best-effort
        print(f"owner notify unavailable: {e}", file=sys.stderr)
    owner_file = Path(__file__).resolve().parents[4] / "docs" / "OWNER_ACTIONS_NOW.md"
    try:
        cur = owner_file.read_text() if owner_file.is_file() else ""
        owner_file.write_text(f"## Release watch ({datetime.now(UTC):%F %T}Z)\n\n{text}\n\n{cur}")
    except OSError as e:
        print(f"owner file unavailable: {e}", file=sys.stderr)


def watch_check() -> int:
    """Cron-able check. Returns 0 healthy, 1 breach (rolled back), 2 no watch."""
    state = load_watch()
    if state is None or state.get("status") != "watching":
        print("no active watch")
        return 2
    res = check(state)
    state["checks"].append({"at": datetime.now(UTC).isoformat(), **res})
    WATCH_FILE.write_text(json.dumps(state, indent=1))
    print("\n".join(res["notes"]))
    if res["breaches"]:
        print("BREACH: " + "; ".join(res["breaches"]))
        from pathlib import Path as P

        from .ship import rollback
        bdirs = sorted(P.home().glob(f".ev/growth/backups/{state['tag']}-*"))
        log = rollback(state["new_sha"], bdirs[-1] if bdirs else P("/nonexistent"),
                       "watch breach: " + "; ".join(res["breaches"]))
        print(log)
        state["status"] = "rolled-back"
        WATCH_FILE.write_text(json.dumps(state, indent=1))
        _notify_owner(f"Release {state['tag']} breached watch ({'; '.join(res['breaches'])}) and was rolled back. DB untouched (manual decision).")
        return 1
    if res["expired"]:
        state["status"] = "complete"
        WATCH_FILE.write_text(json.dumps(state, indent=1))
        _notify_owner(f"Release {state['tag']} completed its 24h watch with no breach.")
        print(f"WATCH COMPLETE for {state['tag']}")
    return 0
