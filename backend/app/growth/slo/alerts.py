"""GE5 alerts: SLO breach checks -> HUD/phone + failure queue + WATCH feed.

`ev growth slo --check` (runs hourly from the growth-watch plist): computes
SLOs, notifies the owner on NEW breaches (deduped by state file), files
each breach in the failure queue, and returns nonzero when breached so
WATCH can roll back.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

STATE = Path.home() / ".ev" / "growth" / "slo-state.json"


async def daily_cost_usd() -> float | None:
    try:
        from sqlalchemy import select

        from app.db import SessionLocal
        from app.gateway.costs import _logged_call_cost
        from app.models import ModelCallLog
    except Exception:
        return None
    try:
        from app.db import utcnow
        cutoff = utcnow() - timedelta(hours=24)
        async with SessionLocal() as session:
            rows = list((await session.execute(
                select(ModelCallLog).where(ModelCallLog.created_at >= cutoff)
            )).scalars().all())
        return round(sum(_logged_call_cost(r) for r in rows), 6)
    except Exception:
        return None


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text()) if STATE.is_file() else {}
    except ValueError:
        return {}


def current() -> dict:
    from . import sink as S
    from .compute import compute

    cost = asyncio.run(daily_cost_usd())
    return compute(S.read_turns(since_h=7 * 24), cost_day_usd=cost)


def _notify(text: str) -> bool:
    try:
        from clients.cli import notify_send
        asyncio.run(notify_send("Evie SLO breach", text, priority=1.0, sticky=True))
        return True
    except Exception as e:  # noqa: BLE001 - best-effort
        print(f"HUD/phone unavailable: {e}", file=sys.stderr)
        return False


def check() -> int:
    from ..learn.queue import record

    rep = current()
    state = load_state()
    prev = set(state.get("breached", []))
    now_breached = {k for k, v in rep["slos"].items() if v["breached"]}
    new = sorted(now_breached - prev)
    for name in new:
        v = rep["slos"][name]
        record("slo-breach", policy=f"{name}={v['value']} target={v['target']} n={v['n']}")
    if new:
        _notify("SLO breach: " + "; ".join(
            f"{n}={rep['slos'][n]['value']} (target {rep['slos'][n]['target']})" for n in new))
        print("BREACHED: " + ", ".join(new))
    else:
        print(f"SLOs ok ({rep['n_7d']} turns/7d; breached: {sorted(now_breached) or 'none'})")
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"breached": sorted(now_breached),
                                 "at": datetime.now(UTC).isoformat()}))
    return 1 if now_breached else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ev growth slo")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.check:
        return check()
    rep = current()
    if a.json:
        print(json.dumps(rep, indent=1))
    else:
        for name, v in rep["slos"].items():
            val = "UNMEASURED" if v["value"] is None else f"{v['value']}"
            print(f"  [{'BREACH' if v['breached'] else 'ok'}] {name}={val} "
                  f"(target {v['target']}, n={v['n']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
