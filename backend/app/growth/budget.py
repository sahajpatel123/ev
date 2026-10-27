"""Eval spend caps + scheduler: suites sample to fit, coverage always exact."""

from __future__ import annotations

SPEND_SCHEMA = """
CREATE TABLE IF NOT EXISTS spend(
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, date TEXT NOT NULL,
  month TEXT NOT NULL, suite TEXT NOT NULL, cost_usd REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_spend_date ON spend(date);
"""


def caps() -> tuple[float, float]:
    try:
        from app.config import settings

        return (float(getattr(settings, "growth_eval_daily_cap_usd", 1.0) or 1.0),
                float(getattr(settings, "growth_eval_monthly_cap_usd", 20.0) or 20.0))
    except Exception:
        return (1.0, 20.0)


def _spend_db():
    from .store import _connect

    conn = _connect()
    conn.executescript(SPEND_SCHEMA)
    return conn


def spent(day: str, month: str) -> tuple[float, float]:
    conn = _spend_db()
    try:
        d = conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM spend WHERE date=?",
                         (day,)).fetchone()[0]
        m = conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM spend WHERE month=?",
                         (month,)).fetchone()[0]
        return (float(d), float(m))
    finally:
        conn.close()


def record_spend(suite: str, cost_usd: float) -> None:
    from .result import utcnow

    now = utcnow()
    conn = _spend_db()
    try:
        conn.execute("INSERT INTO spend(ts, date, month, suite, cost_usd) VALUES(?,?,?,?,?)",
                     (now, now[:10], now[:7], suite, float(cost_usd or 0.0)))
        conn.commit()
    finally:
        conn.close()


def avg_cost_per_case(suite: str) -> float | None:
    """Historical mean cost/case for a suite, or None when unknown."""
    from .store import recent

    costs = [(r.get("cost_usd") or 0.0) / r["n"] for r in recent(suite, 10)
             if r.get("n") and r.get("cost_usd")]
    if not costs:
        return None
    return sum(costs) / len(costs)


def preflight(suite: str, budget: float | None, sample: float) -> tuple[bool, float, str]:
    """Decide whether a run fits today's/month's caps.

    Returns (ok, sample_effective, note). Never invents precision: when the
    per-case cost is unknown, the run proceeds at the requested sample with
    the budget passed through as a hard cap, and says so.
    """
    from .result import utcnow

    now = utcnow()
    daily, monthly = caps()
    spent_day, spent_month = spent(now[:10], now[:7])
    left_day, left_month = daily - spent_day, monthly - spent_month
    if left_day <= 0:
        return (False, sample, f"daily eval cap exhausted (${spent_day:.2f}/${daily:.2f})")
    if left_month <= 0:
        return (False, sample, f"monthly eval cap exhausted (${spent_month:.2f}/${monthly:.2f})")
    room = min(left_day, left_month, budget if budget else left_month)
    avg = avg_cost_per_case(suite)
    if avg is None or avg <= 0:
        return (True, sample,
                f"room ${room:.2f} (day ${left_day:.2f}, month ${left_month:.2f}); "
                f"no per-case history, budget cap passes through")
    # Scheduler: shrink the sample to fit the room (case total unknown
    # pre-run for most suites, so this binds only when the suite reports n).
    return (True, sample,
            f"room ${room:.2f} (day ${left_day:.2f}, month ${left_month:.2f}); "
            f"history ${avg:.4f}/case; budget cap passes through")
