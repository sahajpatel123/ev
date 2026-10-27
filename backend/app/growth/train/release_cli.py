"""`ev release` command surface: submit/gate/rc/shadow/ship/watch/notes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    from .gate import gate_branch
    from .submit import get, scan_mailbox, set_status, submit

    ap = argparse.ArgumentParser(prog="ev release")
    ap.add_argument("cmd", nargs="?", default="inbox",
                    choices=["submit", "gate", "rc", "shadow", "ship", "watch", "notes", "inbox"])
    ap.add_argument("--branch", default=None)
    ap.add_argument("--scope", default="")
    ap.add_argument("--evidence", default="")
    ap.add_argument("--id", dest="sub_id", type=int, default=None)
    ap.add_argument("--budget", type=float, default=2.0)
    ap.add_argument("--out", default=None)
    ap.add_argument("--check", action="store_true")
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
    if a.cmd == "watch" and a.check:
        from .watch import watch_check
        return watch_check()
    sub = get(a.sub_id) if a.sub_id else None
    if sub is None:
        print(f"usage: ev release {a.cmd} --id SUBMISSION_ID")
        return 2
    if a.cmd == "gate":
        print(f"gating submission {a.sub_id} ({sub['branch']}) ...")
        rep = gate_branch(sub["branch"], sub["scope"], sub["base"])
        for name, r in rep["checks"].items():
            print(f"  [{'PASS' if r['pass'] else 'FAIL'}] {name}: {r['detail'][:200]}")
        set_status(a.sub_id, "gate-pass" if rep["pass"] else "gate-blocked", json.dumps(rep)[:4000])
        print("GATE " + ("PASS" if rep["pass"] else "BLOCKED"))
        return 0 if rep["pass"] else 1
    if a.cmd == "rc":
        if sub["status"] != "gate-pass":
            print(f"refusing: status is {sub['status']} (need gate-pass)")
            return 2
        from .rc import run_rc
        out = Path(a.out or f"/tmp/ge-rc-{a.sub_id}")
        print(f"RC battery for submission {a.sub_id} ({sub['branch']}) -> {out} ...")
        rep = run_rc(sub["branch"], sub["base"], out)
        for s, v in rep["suites"].items():
            print(f"  [{v['status']}] {s}: n={v['n']} rate={v['pass_rate']} {v['compare_note'][:100]}")
        for b in rep["blockers"]:
            print(f"  BLOCKER: {b}")
        for g in rep["gaps"]:
            print(f"  GAP: {g}")
        status = {"pass": "rc-pass", "pass-with-gaps": "rc-pass-gaps"}.get(rep["verdict"], "rc-blocked")
        set_status(a.sub_id, status, json.dumps({**rep, "suites": "..."})[:4000])
        print("RC " + rep["verdict"].upper())
        return 0 if rep["verdict"] != "blocked" else 1
    if a.cmd == "shadow":
        if sub["status"] not in ("rc-pass", "rc-pass-gaps"):
            print(f"refusing: status is {sub['status']} (need rc-pass)")
            return 2
        from .gate import ensure_worktree
        from .shadow import extract_turns, shadow_replay
        turns, coverage = extract_turns()
        print(f"shadow turns: {coverage}")
        if not turns:
            set_status(a.sub_id, "shadow-pass", json.dumps({"n_turns": 0, "note": coverage})[:1000])
            print("SHADOW PASS (vacuous: no turns to replay)")
            return 0
        rc_dir = ensure_worktree(sub["branch"])
        base_dir = ensure_worktree(sub["base"])
        rep = shadow_replay(rc_dir, base_dir, turns, a.budget)
        print(f"shadow: pass={rep['pass']} flags={len(rep.get('flags', []))} "
              f"leaks={len(rep.get('leaks', []))} divergences={len(rep.get('divergences', []))}")
        for f in rep.get("flags", []) + rep.get("leaks", []):
            print(f"  FLAG: {f.get('kind')}: {f.get('detail', f.get('op', ''))[:160]}")
        try:
            head = __import__("subprocess").run(
                ["git", "-C", str(Path(__file__).resolve().parents[4]), "rev-parse", sub["branch"]],
                capture_output=True, text=True, check=True).stdout.strip()
        except Exception:
            head = ""
        rep["head"] = head
        set_status(a.sub_id, "shadow-pass" if rep["pass"] else "shadow-blocked",
                   json.dumps(rep)[:4000])
        print("SHADOW " + ("PASS" if rep["pass"] else "BLOCKED"))
        return 0 if rep["pass"] else 1
    if a.cmd == "ship":
        import sys

        from .ship import ship
        if not sys.stdin.isatty():
            print("refusing: `ev release ship` is owner-only and needs a TTY")
            return 2
        phrase = input(f"Type exactly `ship {a.sub_id}` to merge, tag, push, deploy, and watch: ").strip()
        return ship(a.sub_id, confirm_phrase=phrase)
    if a.cmd == "notes":
        from .notes import generate
        print(generate(f"evie-vNEXT (submission {a.sub_id})", sub["branch"], sub["base"],
                       json.loads(sub["report"] or "{}") if sub["status"].startswith("rc-") else {},
                       json.loads(sub["report"] or "{}") if sub["status"].startswith("shadow-") else {}))
        return 0
    if a.cmd == "watch":
        print("usage: ev release watch --check")
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
