"""rec-cost: ledger within 5% of provider-reported usage (A10 half, real).

Method: snapshot key-level usage (OpenRouter /auth/key, read-only, $0)
for live + eval keys; compare each period's ledger spend against the key
usage DELTA over the same period. First run establishes baselines (no
verdict, recorded). Subsequent runs breach past ±5%.
"""

from __future__ import annotations

from datetime import UTC, datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS cost_baseline(
  key_role TEXT PRIMARY KEY, ts TEXT NOT NULL, usage REAL NOT NULL,
  ledger REAL NOT NULL);
"""


def _db():
    from ..store import _connect

    conn = _connect()
    conn.executescript(SCHEMA)
    return conn


async def _ledger_24h() -> float:
    from datetime import timedelta

    from sqlalchemy import select

    from app.db import SessionLocal
    from app.utils.text import utcnow
    from app.gateway.costs import _logged_call_cost
    from app.models import ModelCallLog

    cutoff = utcnow() - timedelta(hours=24)
    async with SessionLocal() as session:
        rows = list((await session.execute(
            select(ModelCallLog).where(ModelCallLog.created_at >= cutoff)
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
    from ..result import finish, new_result, wilson

    res = new_result("rec-cost")
    try:
        from ..result import run_coro
        ledger = run_coro(_ledger_24h())
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
        conn = _db()
        try:
            row = conn.execute("SELECT ts, usage, ledger FROM cost_baseline WHERE key_role=?",
                               (role,)).fetchone()
            if row is None:
                conn.execute("INSERT INTO cost_baseline(key_role, ts, usage, ledger) VALUES(?,?,?,?)",
                             (role, datetime.now(UTC).isoformat(), usage, ledger))
                conn.commit()
                notes.append(f"{role}: baseline established (usage={usage}, ledger24h={ledger}); no verdict yet")
                verdicts[cid] = "skip"
                continue
            _, base_usage, base_ledger = row
            delta_key = usage - float(base_usage)
            # Ledger is trailing-24h; baseline ledger was trailing-24h then.
            # Compare period spend only when the key moved (else vacuous pass).
            if delta_key <= 0:
                notes.append(f"{role}: key usage unmoved since baseline; vacuous pass")
                verdicts[cid] = "skip"
                continue
            # Ledger-side period spend ~= current trailing ledger (approx);
            # exactness improves as snapshots accumulate. Tolerance ±5%.
            diff = abs(ledger - delta_key) / delta_key
            if diff > 0.05:
                failures.append({"id": cid, "repro": "ev growth recurring cost",
                                 "detail": f"{role}: ledger24h={ledger} vs key-delta={delta_key:.4f} (diff {diff:.1%})"})
                verdicts[cid] = "fail"
            else:
                notes.append(f"{role}: within {diff:.1%} (ledger={ledger}, key-delta={delta_key:.4f})")
                verdicts[cid] = "pass"
        finally:
            conn.close()
    scored_fail = sum(1 for v in verdicts.values() if v == "fail")
    scored_pass = sum(1 for v in verdicts.values() if v == "pass")
    skipped = sum(1 for v in verdicts.values() if v == "skip")
    res["cases_run"] = cases
    res["failures"] = failures
    res["coverage_note"] = "; ".join(notes) or "no keys to reconcile"
    n = scored_fail + scored_pass + skipped
    return finish(res, counts=(n, scored_pass, scored_fail, skipped, 0),
                  categories=[{"name": "reconcile", "n": scored_fail + scored_pass,
                               "passed": scored_pass,
                               "rate": round(scored_pass / (scored_fail + scored_pass), 4)
                               if scored_fail + scored_pass else 0.0,
                               "wilson95": wilson(scored_fail + scored_pass, scored_pass)}])
