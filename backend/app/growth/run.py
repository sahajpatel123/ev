"""One dispatcher: `python -m app.growth.run <suite> --budget N --sample S --out PATH`."""

from __future__ import annotations

import argparse
import sys

from .result import write_json
from .store import record
from .suites import cug, gates, gauntlet, gesture, journeys, orb, stubs, unit

SUITES = {
    "unit": unit.run,
    "gates": gates.run,
    "gauntlet": gauntlet.run,
    "cug": cug.run,
    "scorecard": stubs.run_scorecard,
    "journeys": journeys.run,
    "orb": orb.run,
    "gesture": gesture.run,
    "latency": stubs.run_latency,
    "soak": stubs.run_soak,
    "life": stubs.run_life,
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="growth-run")
    ap.add_argument("suite", choices=sorted(SUITES))
    ap.add_argument("--budget", type=float, default=None)
    ap.add_argument("--sample", type=float, default=1.0)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    if not 0.0 < a.sample <= 1.0:
        print("sample must be in (0, 1]", file=sys.stderr)
        return 2
    try:
        res = SUITES[a.suite](budget=a.budget, sample=a.sample, out=a.out)
    except Exception as e:  # noqa: BLE001 - harness errors are exit 1 with reason
        print(f"harness error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    path = write_json(a.out, res)
    try:
        rowid = record(res)
    except Exception as e:  # noqa: BLE001 - result kept, store failure reported
        print(f"result at {path}; metrics store failed: {e}", file=sys.stderr)
        return 1
    status = res.get("status")
    print(f"{a.suite}: status={status} n={res.get('n')} "
          f"pass={res.get('passed')} fail={res.get('failed')} "
          f"skip={res.get('skipped')} ci={res.get('wilson95')} "
          f"cost={res.get('cost_usd')} -> {path} (row {rowid})")
    return 2 if status in ("blocked", "not_built") else 0


if __name__ == "__main__":
    raise SystemExit(main())
