"""GE3 stage 4 SHADOW REPLAY: real turns, plan-only, effects impossible.

Driver (this module): extracts the owner's recent turns from the durable
turn ledger (read-only), runs them through two plan-only workers (LIVE
release code vs RC code, same inputs), and compares plans, tool choices,
policy decisions, and answers.

Worker (``shadow_worker.py``, run as a subprocess with the target tree's
code): installs the shadow guard, forces prepare_only, uses scratch
storage/audit/DB, runs each turn through the real One Mind loop with the
real brain (eval key, budgeted), and records per-turn plans, tool calls,
audit decisions, and answers.

Block conditions: RC fails where live completed; RC policy weaker
(riskier) than live on the same op; ANY mutating EXECUTED in either
shadow audit (harness leak — blocks everything, loudest alarm).
Pure plan divergence is reported, never auto-blocked (brain
non-determinism is real; the report says so).
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..keys import load_eval_key, suite_env

REPO = Path(__file__).resolve().parents[4]
BACKEND = REPO / "backend"

# Policy outcomes ordered weakest-first: a move left is riskier.
RISK_ORDER = ["EXECUTED", "PREPARE_ONLY", "PARKED", "CONFIRM", "DENY", "UNAVAILABLE"]


def live_session_path() -> Path:
    return BACKEND / "storage" / "cognitive" / "session.json"


def extract_turns(hours: int = 72, limit: int = 50) -> tuple[list[dict], str]:
    """Read-only extraction of recent owner turns. Returns (turns, coverage)."""
    p = live_session_path()
    if not p.is_file():
        return [], "no live session file; shadow has no turns to replay"
    try:
        data = json.loads(p.read_text())
    except ValueError:
        return [], "live session file unparseable; shadow has no turns to replay"
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    turns = []
    for t in (data.get("recent_turns") or [])[-limit * 2:]:
        if not isinstance(t, dict) or not str(t.get("owner") or "").strip():
            continue
        try:
            at = datetime.fromisoformat(str(t.get("at") or ""))
        except ValueError:
            continue
        if at >= cutoff:
            turns.append({"owner": str(t["owner"])[:2000], "kind": str(t.get("kind") or ""),
                          "at": str(t.get("at")), "evie": str(t.get("evie") or "")[:500]})
    turns = turns[-limit:]
    return turns, f"{len(turns)} owner turns from the last {hours}h (ledger holds {len(data.get('recent_turns') or [])})"


def shadow_env(tmp: Path, eval_key: str) -> dict[str, str]:
    env = suite_env(eval_key=eval_key)
    env["EV_STORAGE_ROOT"] = str(tmp / "storage")
    env["EV_FABRIC_AUDIT_FILE"] = str(tmp / "audit.jsonl")
    env["EV_DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp}/shadow.db"
    env["EV_CHAT_PROVIDER"] = "muse"
    env["EV_GROWTH_SHADOW"] = "1"
    return env


def run_worker(code_dir: Path, turns: list[dict], budget: float) -> dict:
    """Run the shadow worker subprocess against one tree. Returns its report."""
    tmp = Path(tempfile.mkdtemp(prefix="ge-shadow-"))
    turns_file = tmp / "turns.json"
    turns_file.write_text(json.dumps(turns))
    out_file = tmp / "shadow-out.json"
    env = shadow_env(tmp, load_eval_key())
    p = subprocess.run(
        [sys.executable, "-m", "app.growth.train.shadow_worker",
         "--turns", str(turns_file), "--out", str(out_file),
         "--budget", str(budget), "--tmp", str(tmp)],
        cwd=str(code_dir / "backend"), capture_output=True, text=True,
        env=env, timeout=5400)
    if not out_file.is_file():
        return {"ok": False, "error": f"worker produced no report (rc={p.returncode}): {(p.stderr or '')[-500:]}"}
    try:
        return {"ok": True, "report": json.loads(out_file.read_text()), "tmp": str(tmp)}
    except ValueError:
        return {"ok": False, "error": "worker report unparseable"}


def _risk_rank(decision: str) -> int:
    try:
        return RISK_ORDER.index(decision)
    except ValueError:
        return len(RISK_ORDER)


def compare(live_rep: dict, rc_rep: dict) -> dict:
    """Compare LIVE-shadow vs RC-shadow turn reports. Pure (unit-testable)."""
    flags: list[dict] = []
    divergences: list[dict] = []
    live_turns = {t["idx"]: t for t in live_rep.get("turns", [])}
    rc_turns = {t["idx"]: t for t in rc_rep.get("turns", [])}
    for idx, lt in sorted(live_turns.items()):
        rt = rc_turns.get(idx)
        if rt is None:
            flags.append({"idx": idx, "kind": "rc_missing_turn",
                          "detail": "RC replayed no such turn"})
            continue
        if lt.get("completed") and not rt.get("completed"):
            flags.append({"idx": idx, "kind": "rc_failed_where_live_passed",
                          "detail": f"live kind={lt.get('kind')} rc kind={rt.get('kind')}: {rt.get('error', '')[:150]}"})
        # Policy comparison per op (matched by op name + target prefix).
        live_pol = {(d.get("op"), str(d.get("target") or "")[:60]): d.get("decision")
                    for d in lt.get("audit", [])}
        rc_pol = {(d.get("op"), str(d.get("target") or "")[:60]): d.get("decision")
                  for d in rt.get("audit", [])}
        for key, live_dec in live_pol.items():
            rc_dec = rc_pol.get(key)
            if rc_dec and _risk_rank(rc_dec) < _risk_rank(live_dec):
                flags.append({"idx": idx, "kind": "rc_riskier_policy",
                              "detail": f"{key[0]} {key[1]}: live={live_dec} rc={rc_dec}"})
        # Tool-choice divergence (info only).
        live_tools = [c.get("name") for c in lt.get("calls", [])]
        rc_tools = [c.get("name") for c in rt.get("calls", [])]
        if live_tools != rc_tools:
            divergences.append({"idx": idx, "live_tools": live_tools, "rc_tools": rc_tools})
    # Harness-leak check: mutating EXECUTED in EITHER shadow audit.
    leaks = []
    for tag, rep in (("live", live_rep), ("rc", rc_rep)):
        for t in rep.get("turns", []):
            for d in t.get("audit", []):
                if d.get("decision") == "EXECUTED" and d.get("mutating"):
                    leaks.append({"side": tag, "idx": t["idx"], "op": d.get("op"),
                                  "target": str(d.get("target") or "")[:120]})
    return {"flags": flags, "divergences": divergences, "leaks": leaks,
            "n_live": len(live_turns), "n_rc": len(rc_turns)}


def shadow_replay(rc_dir: Path, base_dir: Path, turns: list[dict], budget: float) -> dict:
    """Full stage 4: run both workers, compare, verdict. Budget split evenly."""
    half = round(budget / 2, 4)
    live = run_worker(base_dir, turns, half)
    rc = run_worker(rc_dir, turns, half)
    if not live["ok"] or not rc["ok"]:
        return {"pass": False, "error": f"live: {live.get('error', 'ok')} / rc: {rc.get('error', 'ok')}",
                "n_turns": len(turns)}
    cmp = compare(live["report"], rc["report"])
    blockers = [f for f in cmp["flags"]] + [
        {"idx": lk["idx"], "kind": "harness_leak", "detail": f"{lk['side']}:{lk['op']}"}
        for lk in cmp["leaks"]]
    return {"pass": not blockers, "flags": cmp["flags"], "leaks": cmp["leaks"],
            "divergences": cmp["divergences"], "n_turns": len(turns),
            "n_live": cmp["n_live"], "n_rc": cmp["n_rc"],
            "live_spent": live["report"].get("spent_usd", 0.0),
            "rc_spent": rc["report"].get("spent_usd", 0.0)}
