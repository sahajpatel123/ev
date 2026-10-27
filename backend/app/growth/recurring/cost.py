"""rec-cost: ledger within 5% of provider-reported usage (A10 half, real).

Method: per-role usage snapshots (OpenRouter /auth/key, read-only, $0).
Each run compares the key-usage DELTA since the last snapshot against the
model-call ledger's spend over the SAME window (exact timestamps, not
trailing-24h approximations), then advances the snapshot. First run per
role establishes the baseline (SKIP, no verdict). Windows under 1h and
unmoved keys SKIP (noise / vacuous); anything else past ±5% FAILS.
"""

from __future__ import annotations

from datetime import UTC, datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS cost_snapshot(
  key_role TEXT PRIMARY KEY, ts TEXT NOT NULL, usage REAL NOT NULL);
"""


def _db():
    from ..store import _connect

    conn = _connect()
    conn.executescript(SCHEMA)
    return conn


async def _ledger_window(since_iso: str) -> float:
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.gateway.costs import _logged_call_cost
    from app.models import ModelCallLog

    since = datetime.fromisoformat(since_iso)
    async with SessionLocal() as session:
        rows = list((await session.execute(
            select(ModelCallLog).where(ModelCallLog.created_at >= since)
        )).scalars().all())
    return round(sum(_logged_call_cost(r) for r in rows), 6)


def _key_usage(key: str) -> float | None:
    try:
        from ..keys import key_credit
        return float(key_credit(key).get("usage") or 0.0)
    except Exception:
        return None


def _live_key() -> str:
    try:
        from app.config import settings
        return str(getattr(settings, "openrouter_api_key", "") or "")
    except Exception:
        return ""


def run() -> dict:
    from ..keys import load_eval_key
    from ..result import finish, new_result, run_coro, wilson

    res = new_result("rec-cost")
    try:
        run_coro(_ledger_window(datetime.now(UTC).isoformat()))  # ledger reachable?
    except Exception as e:  # noqa: BLE001 - ledger unreachable is data
        res["status"] = "blocked"
        res["coverage_note"] = f"model-call ledger unreachable: {type(e).__name__}"[:150]
        return finish(res, counts=(0, 0, 0, 0, 1))
    keys = {"live": _live_key(), "eval": load_eval_key()}
    cases, failures, notes = [], [], []
    verdicts: dict[str, str] = {}
    for role, key in keys.items():
        cid = f"cost-reconcile-{role}"
        cases.append(cid)
        if not key:
            notes.append(f"{role}: no key, skipped")
            verdicts[cid] = "skip"
            continue
        usage = _key_usage(key)
        if usage is None:
            failures.append({"id": cid, "repro": "ev growth recurring cost",
                             "detail": f"{role} key usage endpoint unreachable"})
            verdicts[cid] = "fail"
            continue
        now = datetime.now(UTC)
        conn = _db()
        try:
            row = conn.execute("SELECT ts, usage FROM cost_snapshot WHERE key_role=?",
                               (role,)).fetchone()
            if row is None:
                conn.execute("INSERT INTO cost_snapshot(key_role, ts, usage) VALUES(?,?,?)",
                             (role, now.isoformat(), usage))
                conn.commit()
                notes.append(f"{role}: baseline established (usage={usage}); no verdict yet")
                verdicts[cid] = "skip"
                continue
            since_iso, base_usage = row
            window_h = (now - datetime.fromisoformat(since_iso)).total_seconds() / 3600.0
            if window_h < 1.0:
                notes.append(f"{role}: window {window_h:.1f}h < 1h; too short to judge")
                verdicts[cid] = "skip"
                continue
            try:
                window_spend = run_coro(_ledger_window(since_iso))
            except Exception as e:  # noqa: BLE001 - ledger failure mid-run is data
                failures.append({"id": cid, "repro": "ev growth recurring cost",
                                 "detail": f"{role}: ledger window query failed: {type(e).__name__}"})
                verdicts[cid] = "fail"
                continue
            delta_key = usage - float(base_usage)
            # Advance the snapshot every judged run: the next window starts now.
            conn.execute("UPDATE cost_snapshot SET ts=?, usage=? WHERE key_role=?",
                         (now.isoformat(), usage, role))
            conn.commit()
            if delta_key < 0:
                notes.append(f"{role}: key usage went backwards (key rotated?); re-baselined")
                verdicts[cid] = "skip"
                continue
            if delta_key <= 0 and window_spend == 0:
                notes.append(f"{role}: no spend in {window_h:.1f}h window; nothing to compare")
                verdicts[cid] = "skip"
                continue
            denom = max(delta_key, window_spend)
            diff = abs(window_spend - delta_key) / denom if denom > 0 else 0.0
            if diff > 0.05:
                failures.append({"id": cid, "repro": "ev growth recurring cost",
                                 "detail": f"{role}: ledger-window={window_spend} vs "
                                           f"key-delta={delta_key:.4f} over {window_h:.1f}h "
                                           f"(diff {diff:.1%})"})
                verdicts[cid] = "fail"
            else:
                notes.append(f"{role}: within {diff:.1%} over {window_h:.1f}h "
                             f"(ledger={window_spend}, key-delta={delta_key:.4f})")
                verdicts[cid] = "pass"
        finally:
            conn.close()
    scored_fail = sum(1 for v in verdicts.values() if v == "fail")
    scored_pass = sum(1 for v in verdicts.values() if v == "pass")
    skipped = sum(1 for v in verdicts.values() if v == "skip")
    res["cases_run"] = cases
    res["failures"] = failures
    res["coverage_note"] = "; ".join(notes) or "no keys to reconcile"
    if scored_pass == 0 and scored_fail == 0:
        res["status"] = "partial"  # measured nothing decisive; never "ok"
    n = scored_fail + scored_pass + skipped
    return finish(res, counts=(n, scored_pass, scored_fail, skipped, 0),
                  categories=[{"name": "reconcile", "n": scored_fail + scored_pass,
                               "passed": scored_pass,
                               "rate": round(scored_pass / (scored_fail + scored_pass), 4)
                               if scored_fail + scored_pass else 0.0,
                               "wilson95": wilson(scored_fail + scored_pass, scored_pass)}])
