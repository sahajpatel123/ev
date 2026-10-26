"""Owner-run live checks: `ev hands check permissions|t1|t2` (docs/HANDS_OWNER_CHECKS.md).

These run on the OWNER's real desktop and real windows, so nothing here is part of the test
suite. Everything is a dry run unless a flag says otherwise, and every effect still goes
through the same policy gate and confirm card as production.

    python -m app.fabric.skills.check permissions
    python -m app.fabric.skills.check t1 [--image PATH] [--apply]
    python -m app.fabric.skills.check t2 [--watch SECONDS]
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any

from .api import Hands


def _row(label: str, ok: bool | None, detail: str = "") -> None:
    mark = {True: "ok  ", False: "MISS", None: "?   "}[ok]
    print(f"  [{mark}] {label}{(' - ' + detail) if detail else ''}")


def check_permissions(h: Hands) -> int:
    r = h.permissions()
    print("Permissions (read without triggering any macOS prompt):")
    bad = 0
    for p in r["evidence"]["permissions"]:
        ok = {"granted": True, "denied": False}.get(p["status"])
        bad += ok is False and p["kind"] in ("tcc", "setting")
        _row(p["permission"], ok, p["detail"] + (f"  FIX: {p['fix']}" if p.get("fix") and ok is not True else ""))
    print(r["say"])
    return 1 if bad else 0


def check_t2(h: Hands, watch: float) -> int:
    print("T2 - skip the ad. Needs Chrome or Safari playing a video, JavaScript from Apple Events on.")
    st = h.skill_run("web_media.status")
    print("  status:", st["say"])
    deadline = time.time() + watch
    while True:
        ad = h.skill_run("web_media.ad_state")
        print("  ad:", ad["say"])
        if (ad["evidence"] or {}).get("ad"):
            t0 = time.time()
            r = h.skill_run("web_media.skip_ad")
            print(f"  RESULT ({time.time() - t0:.1f}s): ok={r['ok']} verified={r['verified']} channel={r.get('channel')}")
            print("  SAY:", r["say"])
            if r.get("error"):
                print("  ERROR:", r["error"]["code"], r["error"]["message"], r["error"].get("fix") or "")
            return 0 if r["ok"] else 1
        if time.time() >= deadline:
            print("  No ad right now. Play a YouTube video that shows an ad, or rerun with --watch 120 to wait for one.")
            return 0 if not ad.get("error") else 1
        time.sleep(1.0)


def check_t1(h: Hands, image: str | None, apply: bool) -> int:
    print("T1 - rebuild a terminal layout from a screenshot.")
    args: dict[str, Any] = {"image": image or "last"}
    dry = h.skill_run("coding_sessions.restore_from_screenshot", args, dry_run=True)
    if not dry["ok"]:
        print("  cannot plan:", (dry.get("error") or {}).get("message"), (dry.get("error") or {}).get("fix") or "")
        return 1
    card = dry["confirm"]
    print(f"\n  {card['title']}\n")
    print("\n".join("    " + ln for ln in (card.get("layout_ascii") or "").splitlines()))
    for row in card["rows"]:
        print(f"\n  {row['label']}: cwd={row['cwd']}\n    command: {row['command'] or '(none, plain shell)'}\n"
              f"    grounded: {row['grounded'] or 'no'}  will run: {row['will_run']}"
              + (f"\n    WARN: {'; '.join(row['warnings'])}" if row["warnings"] else ""))
    if not apply:
        print("\n  Dry run only. Nothing was opened. Rerun with --apply to build it (you will be asked to confirm).")
        return 0
    if card.get("physical"):
        print("\n  One command needs a PHYSICAL confirmation; a spoken yes does not count.")
    input("\n  Press Return to open the new tab and resume the sessions above (Ctrl-C to cancel): ")
    r = h.skill_run("coding_sessions.restore_from_screenshot", args, approved=True, approval_id=card["plan_id"])
    print(f"  RESULT: ok={r['ok']} verified={r['verified']}\n  SAY: {r['say']}")
    for p in (r.get("evidence") or {}).get("result", []):
        print(f"    pane {p['pane']}: cwd_ok={p.get('cwd_ok')} echoed={p.get('command_echoed')} "
              f"process_seen={p.get('process_seen')} skipped={p.get('skipped')}")
    if r.get("undo"):
        print("  To undo: close that tab (", r["undo"]["description"], ")")
    return 0 if r["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ev hands check")
    sub = ap.add_subparsers(dest="what", required=True)
    sub.add_parser("permissions")
    p1 = sub.add_parser("t1")
    p1.add_argument("--image")
    p1.add_argument("--apply", action="store_true")
    p2 = sub.add_parser("t2")
    p2.add_argument("--watch", type=float, default=0.0)
    a = ap.parse_args(argv)
    h = Hands()
    if a.what == "permissions":
        return check_permissions(h)
    if a.what == "t2":
        return check_t2(h, a.watch)
    return check_t1(h, a.image, a.apply)


if __name__ == "__main__":
    sys.exit(main())
