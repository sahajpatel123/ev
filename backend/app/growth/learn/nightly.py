"""`ev growth nightly`: mine -> cluster -> draft (opt-out-able, key-gated).

Effective only when the eval key exists AND growth_nightly_enabled is true
(default true = ON for the owner profile). Mines the audit log + turn
ledger since the last run marker, clusters new failures, drafts one
candidate per qualifying cluster. Prints an exact summary; exits 0 with
nothing to do.
"""

from __future__ import annotations

from pathlib import Path

MARKER = Path.home() / ".ev" / "growth" / "nightly-marker"


def enabled() -> tuple[bool, str]:
    try:
        from app.config import settings
        flag = bool(getattr(settings, "growth_nightly_enabled", True))
    except Exception:
        flag = True
    if not flag:
        return False, "growth_nightly_enabled=false (owner opted out)"
    from ..keys import load_eval_key
    if not load_eval_key():
        return False, "no eval key (nightly drafts need it; owner action outstanding)"
    return True, ""


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="ev growth nightly")
    ap.parse_args(argv)
    ok, why = enabled()
    if not ok:
        print(f"nightly skipped: {why}")
        return 0
    from . import candidates as C
    from . import cluster as CL
    from . import mine as M
    from . import queue as Q

    audit_path = Path.home() / ".ev" / "fabric_audit.jsonl"
    ledger_path = Path(__file__).resolve().parents[4] / "backend" / "storage" / "cognitive" / "session.json"
    try:
        since_f = float(MARKER.read_text().strip()) if MARKER.is_file() else 0.0
    except ValueError:
        since_f = 0.0
    audit = [e for e in M.read_audit(audit_path) if M.ts(e) > since_f]
    turns = []
    for t in M.read_ledger(ledger_path):
        try:
            from datetime import datetime
            at = datetime.fromisoformat(str(t.get("at") or "")).timestamp()
        except ValueError:
            continue
        if at > since_f:
            turns.append(t)
    audit, excl_a = M.partition(audit)
    turns, excl_t = M.partition(turns)
    excluded: dict[str, int] = {}
    for d in (excl_a, excl_t):
        for k, v in d.items():
            excluded[k] = excluded.get(k, 0) + v
    found = M.mine_failed_receipts(audit) + M.mine_undos(audit) + M.mine_ledger(turns)
    for f in found:
        Q.record(f["source"], prov="owner",
                 **{k: str(v) for k, v in f.items() if k != "source"})
    groups = CL.cluster(found)
    drafted = 0
    for cid, members in CL.rank(groups):
        if len(members) >= 2 or cid.startswith(("denied::", "failed-receipt::")):
            C.draft_from_cluster(cid, members)
            drafted += 1
    import time
    MARKER.parent.mkdir(parents=True, exist_ok=True)
    MARKER.write_text(f"{time.time():.0f}")
    print(f"nightly: {len(found)} failures mined ({len(turns)} owner turns, {len(audit)} owner audit rows; "
          f"excluded by provenance: {excluded or 'none'}), "
          f"{len(groups)} clusters, {drafted} candidates drafted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
