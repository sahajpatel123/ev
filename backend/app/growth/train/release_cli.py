"""`ev release` command surface (submit/gate/inbox today; rc/shadow/ship/watch/notes land next)."""

from __future__ import annotations

import argparse
import json


def main(argv: list[str] | None = None) -> int:
    from .gate import gate_branch
    from .submit import get, scan_mailbox, set_status, submit

    ap = argparse.ArgumentParser(prog="ev release")
    ap.add_argument("cmd", nargs="?", default="inbox", choices=["submit", "gate", "inbox"])
    ap.add_argument("--branch", default=None)
    ap.add_argument("--scope", default="")
    ap.add_argument("--evidence", default="")
    ap.add_argument("--id", dest="sub_id", type=int, default=None)
    a = ap.parse_args(argv)
    if a.cmd == "inbox":
        offers = scan_mailbox()
        if not offers:
            print("no SUBMIT offers in the captain mailbox.")
            return 0
        for o in offers:
            print(f"--- {o['file']}\n{o['head'][:400]}")
        return 0
    if a.cmd == "submit":
        if not a.branch or not a.scope:
            print("usage: ev release submit --branch B --scope 'paths,...' --evidence '...'")
            return 2
        try:
            r = submit(a.branch, a.scope, a.evidence)
        except ValueError as e:
            print(f"submit refused: {e}")
            return 2
        print(f"submission {r['id']}: {r['branch']} ({r['files']} files) -> GATE with --id {r['id']}")
        return 0
    # gate
    sub = get(a.sub_id) if a.sub_id else None
    if sub is None:
        print("usage: ev release gate --id SUBMISSION_ID")
        return 2
    print(f"gating submission {a.sub_id} ({sub['branch']}) ...")
    rep = gate_branch(sub["branch"], sub["scope"], sub["base"])
    for name, r in rep["checks"].items():
        print(f"  [{'PASS' if r['pass'] else 'FAIL'}] {name}: {r['detail'][:160]}")
    set_status(a.sub_id, "gate-pass" if rep["pass"] else "gate-blocked", json.dumps(rep)[:4000])
    print("GATE " + ("PASS" if rep["pass"] else "BLOCKED"))
    return 0 if rep["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
