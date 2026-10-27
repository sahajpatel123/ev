"""`ev review`: the owner's few-minutes-a-week candidate triage.

Lists PENDING-REVIEW candidates one at a time: approve (a), reject (r),
skip (s), quit (q). Approving a lane-level case (files/sql) also files it
into the LIFE suite; anything else parks as approved-but-unrunnable with
its reason (the LIFE runner reports it as a skip, never a fake pass).
Owner-only: refuses without a TTY.
"""

from __future__ import annotations

import json
import sys


def review_loop(limit: int = 20) -> int:
    from . import candidates as C
    from . import life_cases as L

    if not sys.stdin.isatty():
        print("refusing: `ev review` is owner-only and needs a TTY")
        return 2
    items = C.pending()[:limit]
    if not items:
        print("no pending candidates — nothing to review.")
        return 0
    print(f"{len(items)} candidate(s). a=approve r=reject s=skip q=quit\n")
    counts = {"approved": 0, "rejected": 0, "skipped": 0}
    for c in items:
        setup = json.loads(c["setup"] or "{}")
        oracle = json.loads(c["oracle"] or "{}")
        print(f"--- candidate {c['id']}: {c['title']}")
        print(f"    cluster: {c['cluster_id']}  ts: {c['ts']}")
        print(f"    setup: {json.dumps(setup)[:300]}")
        print(f"    oracle: {json.dumps(oracle)[:300]}")
        lane = input("    lane for LIFE runner (files/sql/other)? [other] ").strip().lower() or "other"
        op = ""
        if lane in ("files", "sql"):
            op = input("    op (files: create/read/list/delete/undo; sql: read)? ").strip().lower()
        ans = input("    [a]pprove [r]eject [s]kip [q]uit? ").strip().lower()
        if ans == "q":
            break
        if ans == "r":
            C.set_status(c["id"], "rejected")
            counts["rejected"] += 1
            continue
        if ans != "a":
            counts["skipped"] += 1
            continue
        C.set_status(c["id"], "approved")
        counts["approved"] += 1
        if (lane, op) in L.RUNNABLE:
            expect_ok = input("    expect ok=true? [Y/n] ").strip().lower() != "n"
            raw_args = input('    call args JSON list (e.g. ["note.txt"])? ').strip() or "[]"
            try:
                call_args = json.loads(raw_args)
                assert isinstance(call_args, list)
            except (ValueError, AssertionError):
                call_args = []
                print("    (unparseable args; filed with [] — case will fail until fixed)")
            setup_raw = input("    setup files (name=body, comma-separated, empty if none)? ").strip()
            setup_files = {}
            for piece in setup_raw.split(","):
                if "=" in piece:
                    k, v = piece.split("=", 1)
                    setup_files[k.strip()] = v
            call = {"args": call_args, "kwargs": {}}
            if lane == "sql":
                sql = input("    SQL to run (default SELECT * FROM t)? ").strip() or "SELECT * FROM t"
                L.add(c["id"], c["title"], lane, op,
                      {"sql": sql, "setup_sql": []}, {"ok": expect_ok})
            else:
                L.add(c["id"], c["title"], lane, op,
                      {"call": call, "setup_files": setup_files}, {"ok": expect_ok})
            print(f"    filed as LIFE case (runs as {lane}.{op})")
        else:
            L.add(c["id"], c["title"], lane or "other", op or "-",
                  {}, {"note": "approved-but-unrunnable in runner v1"})
            print("    parked as approved-but-unrunnable (reported as skip with reason)")
    print(f"\napproved={counts['approved']} rejected={counts['rejected']} skipped={counts['skipped']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="ev review")
    ap.add_argument("--limit", type=int, default=20)
    a = ap.parse_args(argv)
    return review_loop(limit=a.limit)


if __name__ == "__main__":
    raise SystemExit(main())
