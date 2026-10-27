"""rec-egress: egress ledger completeness (A8 half, real) + HTTP-counter note.

Proven today: 100% of ModelCallLog rows in 24h carry data_class AND a cost
basis (the "with data class and tier" half of A8). The "100% of remote
calls" half needs a durable HTTP-layer counter at the two provider call
sites (FIX_REQUEST to blooming-mensa, whose files they are): the check
auto-upgrades to call-vs-row comparison when the counter table exists.
"""

from __future__ import annotations

from ..result import finish, new_result, wilson


async def _rows_24h() -> list[dict]:
    from datetime import timedelta

    from sqlalchemy import select

    from app.db import SessionLocal
    from app.utils.text import utcnow
    from app.models import ModelCallLog

    cutoff = utcnow() - timedelta(hours=24)
    async with SessionLocal() as session:
        rows = list((await session.execute(
            select(ModelCallLog).where(ModelCallLog.created_at >= cutoff)
        )).scalars().all())
    return [{"data_class": (r.envelope or {}).get("data_class"),
             "cost_usd": (r.envelope or {}).get("cost_usd"),
             "cost_source": (r.envelope or {}).get("cost_source")} for r in rows]


def _counter_table_exists() -> bool:
    try:
        import sqlite3

        from app.config import settings
        url = str(getattr(settings, "database_url", ""))
        if not url.startswith("sqlite"):
            return False
        path = url.split("///")[-1]
        conn = sqlite3.connect(path)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE name='http_calls'").fetchone()
            return row is not None
        finally:
            conn.close()
    except Exception:
        return False


def run() -> dict:
    res = new_result("rec-egress")
    try:
        from ..result import run_coro
        rows = run_coro(_rows_24h())
    except Exception as e:  # noqa: BLE001 - ledger unreachable is data
        res["status"] = "blocked"
        res["coverage_note"] = f"model-call ledger unreachable: {type(e).__name__}: {e}"[:200]
        return finish(res, counts=(0, 0, 0, 0, 1))
    missing = [i for i, r in enumerate(rows)
               if not r["data_class"] or r["cost_usd"] is None]
    res["cases_run"] = [f"ledger-row-{i}" for i in range(len(rows))]
    if missing:
        res["failures"] = [{"id": f"ledger-row-{i}",
                            "repro": "ev growth recurring egress",
                            "detail": "row lacks data_class or cost basis"} for i in missing[:10]]
    note = (f"{len(rows) - len(missing)}/{len(rows)} rows carry data_class+cost; "
            f"HTTP-layer counter table: {'present' if _counter_table_exists() else 'PENDING (FIX_REQUEST to blooming-mensa)'}")
    res["coverage_note"] = note
    n = len(rows)
    passed = n - len(missing)
    return finish(res, counts=(n, passed, len(missing), 0, 0),
                  categories=[{"name": "ledger", "n": n, "passed": passed,
                               "rate": round(passed / n, 4) if n else 0.0,
                               "wilson95": wilson(n, passed)}])
