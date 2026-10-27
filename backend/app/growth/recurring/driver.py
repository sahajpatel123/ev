"""GE8 recurring checks: one-off proofs turned permanent.

Each check returns the standard result (suite rec-<name>) and records it
in the metrics store. `ev growth recurring [name]`: daily driver (all but
soak); soak runs weekly (120 min) with a 5-minute machinery smoke.
"""

from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> int:
    from ..result import utcnow  # noqa: F401
    from ..store import record
    from . import cost, egress, fabric_legacy, idle, models_empty, shell_drill, soak

    CHECKS = {
        "models-empty": models_empty.run,
        "fabric-legacy": fabric_legacy.run,
        "egress": egress.run,
        "cost": cost.run,
        "shell-drill": shell_drill.run,
        "soak": soak.run,
        "idle-app": idle.run_app,
        "idle-backend": idle.run_backend,
    }
    ap = argparse.ArgumentParser(prog="ev growth recurring")
    ap.add_argument("name", nargs="?", default="daily",
                    choices=[*CHECKS, "daily", "all"])
    ap.add_argument("--soak-minutes", type=float, default=120.0)
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    names = [n for n in CHECKS if n != "soak"] if a.name == "daily" else (
        list(CHECKS) if a.name == "all" else [a.name])
    rc = 0
    for name in names:
        try:
            res = CHECKS[name](minutes=a.soak_minutes) if name == "soak" else CHECKS[name]()
        except Exception as e:  # noqa: BLE001 - check failure is data
            from ..result import finish, new_result
            res = new_result(f"rec-{name}")
            res["status"] = "blocked"
            res["coverage_note"] = f"check crashed: {type(e).__name__}: {e}"[:300]
            res = finish(res, counts=(0, 0, 0, 0, 1))
        record(res)
        if a.out:
            from pathlib import Path

            from ..result import write_json
            dest = a.out if len(names) == 1 else str(Path(a.out) / f"{res['suite']}.json")
            write_json(dest, res)
            print(f"wrote {dest}")
        print(f"{res['suite']}: {res['status']} n={res['n']} pass={res['passed']} "
              f"fail={res['failed']} skip={res['skipped']} :: {res['coverage_note'][:160]}")
        if res["status"] == "blocked" or res["failed"]:
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
