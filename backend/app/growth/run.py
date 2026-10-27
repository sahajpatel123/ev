"""One dispatcher: `python -m app.growth.run <suite> --budget N --sample S --out PATH`."""

from __future__ import annotations

import argparse
import sys

from .budget import preflight, record_spend, room_usd
from .keys import load_eval_key, needs_eval_key
from .result import finish, new_result, write_json
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
    if a.budget is not None and a.budget < 0:
        print("budget must be >= 0", file=sys.stderr)
        return 2
    if needs_eval_key(a.budget) and not load_eval_key():
        res = new_result(a.suite, status="blocked", sample=a.sample, budget=a.budget)
        res["coverage_note"] = ("spend requested but no eval key: run `ev keys set openrouter-eval`; "
                                "the live key is never used by suites")
        res = finish(res, counts=(0, 0, 0, 0, 0))
        path = write_json(a.out, res)
        print(f"{a.suite}: blocked (no eval key) -> {path}")
        return 2
    ok, sample_eff, note = preflight(a.suite, a.budget, a.sample)
    if not ok:
        res = new_result(a.suite, status="blocked", sample=a.sample, budget=a.budget)
        res["coverage_note"] = note
        res = finish(res, counts=(0, 0, 0, 0, 0))
        path = write_json(a.out, res)
        print(f"{a.suite}: blocked ({note}) -> {path}")
        return 2
    # Free paths (None) spend nothing unbudgeted; else clamp to the room.
    effective = None if a.budget is None else max(min(a.budget, room_usd(a.budget)), 0.0)
    try:
        res = SUITES[a.suite](budget=effective, sample=sample_eff, out=a.out)
    except Exception as e:  # noqa: BLE001 - harness errors are exit 1 with reason
        print(f"harness error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    res["coverage_note"] = f"{note}; {res.get('coverage_note', '')}".strip("; ")
    path = write_json(a.out, res)
    try:
        rowid = record(res)
        record_spend(a.suite, res.get("cost_usd") or 0.0)
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
