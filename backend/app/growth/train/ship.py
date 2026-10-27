"""GE3 stage 5 SHIP: the ONE command the owner runs (`ev release ship`).

Preconditions (all enforced, no overrides):
- Submission status is ``shadow-pass`` (gate -> rc -> shadow, in order).
- Branch HEAD equals the SHA the shadow stage tested (moved branch = re-run).
- The branch diff's base is still main's HEAD (no drift under the release).
- stdin is a TTY and the owner types the exact confirmation phrase.

Then: merge (no-ff) -> tag evie-v1.x.y -> push main+tag (owner's
credentials, owner's action) -> backups -> app rebuild only if the branch
touches client code -> deploy wrapper -> verify live -> enter WATCH.

Rollback is automatic on any post-merge failure: services revert via a
revert-commit + wrapper redeploy, Evie.app restores from backup. The
database is NEVER auto-restored (forward-only; the verified dump exists
and manual restore is an explicit owner decision).
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
WRAPPER = REPO / "scripts" / "deploy_production.sh"
BACKUP_ROOT = Path.home() / ".ev" / "growth" / "backups"


def _git(*a: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(REPO), *a], capture_output=True,
                          text=True, check=check)


def next_version() -> str:
    tags = _git("tag", "--list", "evie-v*").stdout.split()
    nums = []
    for t in tags:
        parts = t.replace("evie-v", "").split(".")
        try:
            nums.append(tuple(int(x) for x in parts[:3]))
        except ValueError:
            continue
    major, minor, patch = max(nums) if nums else (1, 0, 0)
    return f"evie-v{major}.{minor}.{patch + 1}"


def touches_client(branch: str, base: str) -> bool:
    out = _git("diff", "--name-only", f"{base}...{branch}").stdout.splitlines()
    return any(f.startswith(("macos/", "clients/", "ios/", "pwa/")) or "Evie.app" in f for f in out)


def backup(tag: str, live_sha: str) -> Path:
    """Back up live state. Returns the backup dir. Raises on failure."""
    d = BACKUP_ROOT / f"{tag}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    d.mkdir(parents=True)
    (d / "LIVE_SHA").write_text(live_sha + "\n")
    (d / "main-sha").write_text(_git("rev-parse", "main").stdout)
    # Owner DB dump (Postgres live). Verified readable, never auto-restored.
    dump = d / "db.dump"
    p = subprocess.run(["pg_dump", "-Fc", "-f", str(dump)], capture_output=True,
                       text=True, timeout=900)
    if p.returncode != 0 or not dump.is_file():
        raise RuntimeError(f"db dump failed: {(p.stderr or '')[-300:]}")
    v = subprocess.run(["pg_restore", "--list", str(dump)], capture_output=True,
                       text=True, timeout=300)
    if v.returncode != 0 or not v.stdout.strip():
        raise RuntimeError("db dump unreadable by pg_restore --list")
    # launchd plists.
    for plist in Path.home().glob("Library/LaunchAgents/ev.*.plist"):
        (d / plist.name).write_bytes(plist.read_bytes())
    # Evie.app binary (rollback path for the client).
    app = Path("/Applications/Evie.app")
    if app.exists():
        p = subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", str(app),
                            str(d / "Evie.app.zip")], capture_output=True, text=True,
                           timeout=900)
        if p.returncode != 0:
            raise RuntimeError(f"Evie.app backup failed: {(p.stderr or '')[-200:]}")
    return d


def verify_live(expected_sha: str) -> tuple[bool, list[str]]:
    """Post-deploy live battery. Returns (ok, notes)."""
    import urllib.request

    notes = []

    def get(path: str, port: int = 8000, timeout: int = 10):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
                return json.load(r)
        except Exception as e:
            return {"_error": f"{type(e).__name__}: {e}"}

    ok = True
    for port, name in ((8000, "api"), (18000, "talk")):
        h = get("/v1/health", port)
        sha = (h.get("git", {}) or {}).get("sha", "")[:10]
        match = sha == expected_sha[:10]
        ok = ok and match
        notes.append(f"{name} health sha={sha or h.get('_error', '?')} {'OK' if match else 'MISMATCH'}")
    st = get("/v1/fabric/status")
    brain = st.get("brain") or st.get("model") or "?"
    notes.append(f"fabric status brain={brain} commit={(st.get('commit') or '')[:10]}")
    if not st or "_error" in st:
        ok = False
    return ok, notes


def rollback(merge_sha: str, backup_dir: Path, reason: str) -> str:
    """Revert the merge on main, push, redeploy, restore the app. Returns a log."""
    log = [f"ROLLBACK: {reason}"]
    _git("revert", "-m", "1", "--no-edit", merge_sha, check=True)
    p = _git("push", "origin", "main")
    log.append(f"push revert: rc={p.returncode} {(p.stderr or '')[-200:]}")
    p = subprocess.run([str(WRAPPER)], capture_output=True, text=True, timeout=900)
    log.append(f"wrapper redeploy: rc={p.returncode} {(p.stdout or p.stderr or '')[-300:]}")
    appzip = backup_dir / "Evie.app.zip"
    if appzip.is_file():
        subprocess.run(["rm", "-rf", "/Applications/Evie.app"], timeout=300)
        p = subprocess.run(["ditto", "-x", "-k", str(appzip), "/Applications"],
                           capture_output=True, text=True, timeout=900)
        log.append(f"app restore: rc={p.returncode}")
    log.append("DB NOT auto-restored (forward-only); verified dump at " + str(backup_dir / "db.dump"))
    return "\n".join(log)


def ship(sub_id: int, *, confirm_phrase: str = "", version: str = "") -> int:
    """Run the ship flow. Returns process exit code. confirm_phrase must be
    typed by the owner (``ship <id>``); empty in non-interactive contexts."""
    from .submit import get, set_status

    sub = get(sub_id)
    if sub is None:
        print(f"no submission {sub_id}", file=sys.stderr)
        return 2
    if sub["status"] != "shadow-pass":
        print(f"refusing: submission {sub_id} status is {sub['status']} (need shadow-pass)",
              file=sys.stderr)
        return 2
    try:
        tested = json.loads(sub["report"] or "{}").get("head", "")
    except ValueError:
        tested = ""
    head = _git("rev-parse", sub["branch"]).stdout.strip()
    if not tested or head != tested:
        print(f"refusing: branch moved (tested {tested[:10] or '?'} vs head {head[:10]}); re-run RC+shadow",
              file=sys.stderr)
        return 2
    if not sys.stdin.isatty():
        print("refusing: `ev release ship` is owner-only and needs a TTY confirmation",
              file=sys.stderr)
        return 2
    if confirm_phrase != f"ship {sub_id}":
        print(f"type exactly: ship {sub_id}", file=sys.stderr)
        return 2
    # Merge, tag, push.
    _git("checkout", "main", check=True)
    _git("pull", "--ff-only", "origin", "main", check=True)
    m = _git("merge", "--no-ff", "--no-edit", sub["branch"])
    if m.returncode != 0:
        _git("merge", "--abort")
        print(f"merge failed: {(m.stderr or '')[-500:]}", file=sys.stderr)
        return 1
    merge_sha = _git("rev-parse", "HEAD").stdout.strip()
    tag = version or next_version()
    _git("tag", tag, check=True)
    p = _git("push", "origin", "main", tag)
    if p.returncode != 0:
        print(f"push failed: {(p.stderr or '')[-300:]}", file=sys.stderr)
        return 1
    # Backups, deploy, verify.
    try:
        live_sha = _git("rev-parse", "HEAD~1").stdout.strip()
        bdir = backup(tag, live_sha)
    except RuntimeError as e:
        print(f"backup failed, NOT deploying: {e}", file=sys.stderr)
        return 1
    if touches_client(sub["branch"], sub["base"]):
        print("branch touches client code: rebuilding Evie.app...")
        p = subprocess.run(["zsh", "macos/scripts/package.sh"], cwd=str(REPO),
                           capture_output=True, text=True, timeout=1800)
        if p.returncode != 0:
            print(rollback(merge_sha, bdir, f"app build failed: {(p.stderr or '')[-300:]}"))
            set_status(sub_id, "ship-rolled-back", "app build failed")
            return 1
        p = subprocess.run(["zsh", "macos/scripts/install.sh"], cwd=str(REPO),
                           capture_output=True, text=True, timeout=900)
        if p.returncode != 0:
            print(rollback(merge_sha, bdir, f"app install failed: {(p.stderr or '')[-300:]}"))
            set_status(sub_id, "ship-rolled-back", "app install failed")
            return 1
    p = subprocess.run([str(WRAPPER)], capture_output=True, text=True, timeout=900)
    if p.returncode != 0:
        print(rollback(merge_sha, bdir, f"deploy wrapper failed: {(p.stderr or '')[-300:]}"))
        set_status(sub_id, "ship-rolled-back", "deploy wrapper failed")
        return 1
    ok, notes = verify_live(merge_sha)
    print("\n".join(notes))
    if not ok:
        print(rollback(merge_sha, bdir, "post-deploy verification failed"))
        set_status(sub_id, "ship-rolled-back", "verify live failed")
        return 1
    from .watch import enter_watch
    enter_watch(tag, live_sha, merge_sha)
    set_status(sub_id, "watching", json.dumps({"tag": tag, "head": merge_sha})[:2000])
    print(f"SHIPPED {tag} {merge_sha[:10]}; WATCH entered (24h)")
    return 0
