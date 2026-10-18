"""``make live-proof`` — run LP1-LP10 against real Mac lanes and write docs/LIVE_PROOF.md.

Every proof runs the **real** lane (policy + receipts), never a double:

* files land only under ``~/Desktop/evie-live-proof/`` (R2);
* shell commands are harmless ``echo``s;
* PIM proofs use a throwaway Notes folder / "Evie Live Proof" Reminders list
  and clean up after themselves;
* SQL proofs use a scratch SQLite file and a throwaway Postgres container.

Nothing here touches the owner's running Evie, its database, or its vault.
Proofs that cannot run because a permission is missing report ``OWNER-ACTION``
with the exact fix — they never report PASS (R9).

Usage::

    python -m app.scripts.live_proof                # every LP, write the doc
    python -m app.scripts.live_proof --only lp8,lp9
    python -m app.scripts.live_proof --stdout       # don't touch the file
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
DOC = ROOT / "docs" / "LIVE_PROOF.md"
PROOF_ROOT = Path.home() / "Desktop" / "evie-live-proof"

PASS, FAIL, OWNER = "PASS", "FAIL", "OWNER-ACTION"


def now() -> str:
    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Proof:
    """One live proof: a title, some output blocks, a verdict."""

    def __init__(self, lp: str, title: str) -> None:
        self.lp = lp
        self.title = title
        self.stamp = now()
        self.blocks: list[str] = []
        self.verdict = PASS
        self.owner_fix = ""
        self.detail = ""

    def note(self, text: str) -> None:
        self.blocks.append(text.rstrip())

    def block(self, text: str, lang: str = "text") -> None:
        self.blocks.append(f"```{lang}\n{text.rstrip()}\n```")

    def owner(self, fix: str, detail: str = "") -> Proof:
        self.verdict = OWNER
        self.owner_fix = fix
        self.detail = detail
        return self

    def fail(self, detail: str) -> Proof:
        self.verdict = FAIL
        self.detail = detail
        return self

    def render(self) -> str:
        head = f"## {self.lp.upper()} {self.title} — {self.verdict}"
        out = [head, ""]
        if self.stamp:
            out += [f"{self.stamp}.", ""]
        out += self.blocks
        if self.detail:
            out += ["", f"**{self.verdict}:** {self.detail}"]
        if self.owner_fix:
            out += ["", f"**OWNER-ACTION:** {self.owner_fix}"]
        return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# environment
# --------------------------------------------------------------------------- #

def scratch_root() -> Path:
    PROOF_ROOT.mkdir(parents=True, exist_ok=True)
    return PROOF_ROOT


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def host_line() -> str:
    ver = _run(["sw_vers", "-productVersion"]).stdout.strip() or "unknown"
    return (f"Host: macOS {ver} {os.uname().machine}, "
            f"Python {sys.version.split()[0]}.")


@contextlib.asynccontextmanager
async def scratch_session():
    """A throwaway SQLite database with the real schema. Never production."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models import Base

    tmp = PROOF_ROOT / "lp-scratch"
    tmp.mkdir(parents=True, exist_ok=True)
    db = tmp / f"lp-{int(time.time())}-{os.getpid()}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db}", future=True)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as session:
            yield session
    finally:
        await engine.dispose()
        with contextlib.suppress(OSError):
            db.unlink()


async def lane(name: str, args: dict[str, Any], *, actor: str = "master",
               approved: bool = True, session=None) -> dict[str, Any]:
    """Call a real fabric dispatch tool, exactly as the kernel would."""
    from app.fabric.dispatch_tools import run_fabric_tool

    if session is not None:
        return await run_fabric_tool(session, name, args, actor=actor,
                                     approved=approved)
    async with scratch_session() as s:
        return await run_fabric_tool(s, name, args, actor=actor, approved=approved)


def bridge_ok() -> bool:
    try:
        from app.fabric.bridge_client import BridgeClient

        return bool(BridgeClient(timeout=6).call("ping").get("ok"))
    except Exception:
        return False


def ensure_evie() -> bool:
    """Bring Evie.app up if it is down. Returns True when the bridge answers."""
    if bridge_ok():
        return True
    with contextlib.suppress(Exception):
        _run(["open", "-a", "Evie"], timeout=30)
    deadline = time.time() + 25
    while time.time() < deadline:
        if bridge_ok():
            return True
        time.sleep(2)
    return False


EVIE_FIX = "Launch Evie.app (open -a Evie), then re-run `make live-proof`."
NOTES_FIX = ("System Settings > Privacy & Security > Automation > Evie > enable "
             "Notes, then re-run `make live-proof`.")
SCREEN_FIX = ("System Settings > Privacy & Security > Screen Recording > enable "
              "Evie, then re-run `make live-proof`. `ev setup` polls that pane.")


def _brief(receipt: dict) -> str:
    """One line of a receipt. Never includes file bodies that might be secrets."""
    ev = receipt.get("evidence") or {}
    keep = {}
    for key in ("sha256", "size", "cmd", "class", "needs_confirm", "exit_code",
                "preview", "kind", "affected_rows", "rolled_back", "rows",
                "session", "window_id", "state", "mode", "n", "container"):
        if key in ev:
            keep[key] = ev[key]
    out = ev.get("out")
    if isinstance(out, str) and out.strip():
        keep["out"] = out.strip().splitlines()[0][:120]
    return (f"ok={receipt.get('ok')} verified={receipt.get('verified')} "
            f"error={receipt.get('error') or ''} evidence={keep}")


# --------------------------------------------------------------------------- #
# LP1–LP7, LP10 — real lanes, harmless effects (R2)
# --------------------------------------------------------------------------- #

def lp1() -> Proof:
    """Create, overwrite, undo, delete-to-trash, undo. Read back every step."""
    from app.fabric import files

    p = Proof("lp1", "real file create/edit/undo/delete-to-trash")
    root = scratch_root()
    rel = "lp1.txt"
    target = root / rel
    target.unlink(missing_ok=True)
    created = files.create(rel, "hello live", root=root)
    if not created.get("ok") or not created.get("verified"):
        return p.fail(f"create: {_brief(created)}")
    over = files.overwrite(rel, "hello overwritten", root=root)
    undone = files.undo(over.get("undo") or {}, root=root)
    read = files.read_file(rel, root=root)
    body = (read.get("evidence") or {}).get("body")
    deleted = files.delete(rel, root=root)
    restored = files.undo(deleted.get("undo") or {}, root=root)
    final = files.read_file(rel, root=root)
    lines = [
        f"create: {created.get('ok')} {created.get('verified')} "
        f"{(created.get('evidence') or {}).get('sha256', '')[:12]}",
        f"overwrite: {over.get('ok')} {over.get('verified')}",
        f"undo-overwrite: {undone.get('ok')} {undone.get('verified')}",
        f"read-back: {body!r}",
        f"delete-to-trash: {deleted.get('ok')} {deleted.get('verified')}",
        f"undo-delete: {restored.get('ok')} {restored.get('verified')}",
        f"final-exists: {(final.get('evidence') or {}).get('body') == 'hello live'}",
    ]
    p.block("\n".join(lines))
    p.note("Every step is the files lane receipt. verified is true only after a read-back.")
    good = (body == "hello live" and deleted.get("verified") and restored.get("verified")
            and (final.get("evidence") or {}).get("body") == "hello live")
    if not good:
        p.fail("a step's read-back did not match the receipt")
    target.unlink(missing_ok=True)
    return p


def lp2() -> Proof:
    """A tmux-backed session started by one object is readable from another."""
    import time as _time

    from app.fabric.shell_sessions import ShellSessions

    p = Proof("lp2", "long-running shell session survives API restart")
    root = scratch_root()
    started = ShellSessions(root=root).start(
        "echo LP2-ALIVE && sleep 20", confirm=True)
    if not started.get("ok"):
        return p.fail(f"start: {_brief(started)}")
    sid = started.get("session_id")
    output = ""
    alive = False
    try:
        deadline = _time.time() + 8
        while _time.time() < deadline:
            again = ShellSessions(root=root).reattach(sid)
            output = str(again.get("output") or "")
            alive = bool(again.get("ok"))
            if alive and "LP2-ALIVE" in output:
                break
            _time.sleep(0.3)
    finally:
        ShellSessions(root=root).cancel(sid)
    lines = [
        f"started: {started.get('ok')} {sid}",
        f"reattached: {alive} marker: {'LP2-ALIVE' in output}",
    ]
    p.block("\n".join(lines))
    p.note("The second ShellSessions is a new object. The on-disk registry is the only memory, "
           "which is what an API restart has.")
    if not (alive and "LP2-ALIVE" in output):
        p.fail(f"reattach missed the marker: {output[:200]!r}")
    return p


def lp3() -> Proof:
    """Secrets are denied. Outside the proof root parks. echo is allowed."""
    from app.fabric import files
    from app.fabric.shell_sessions import ShellSessions

    p = Proof("lp3", "policy denies secrets and escalates outside-root")
    root = scratch_root()
    secret = str(Path.home() / ".ssh" / "evie-live-proof-denied")
    denied = files.create(secret, "nope", root=root)
    denied_read = files.read_file(secret, root=root)
    outside = files.read_file("/etc/hosts", root=root)
    shell = ShellSessions(root=root)
    sudo = shell.run("sudo -n true")
    touch = shell.run("touch lp3-touch.txt")
    echo = shell.run("echo LP3-OK")
    secret_gone = not Path(secret).exists()
    lines = [
        f"secret-create: {denied.get('error')} ok={denied.get('ok')}",
        f"secret-read: {denied_read.get('error')} ok={denied_read.get('ok')}",
        f"outside-read: {outside.get('error')} confirm={outside.get('confirm')}",
        f"shell-sudo: {sudo.get('error')}",
        f"shell-touch: {touch.get('error')} needs_confirm="
        f"{(touch.get('evidence') or {}).get('needs_confirm')}",
        f"shell-echo: {((echo.get('evidence') or {}).get('out') or '').strip()}",
        f"secret-file-absent: {secret_gone}",
    ]
    p.block("\n".join(lines))
    touch_parked = touch.get("ok") is False and (touch.get("evidence") or {}).get("needs_confirm")
    good = (denied.get("ok") is False and denied.get("error") == "secret path denied"
            and denied_read.get("ok") is False and secret_gone
            and outside.get("ok") is False and outside.get("confirm") is True
            and sudo.get("ok") is False and "denied" in str(sudo.get("error") or "")
            and touch_parked and not (root / "lp3-touch.txt").exists()
            and "LP3-OK" in str((echo.get("evidence") or {}).get("out") or ""))
    if not good:
        p.fail("a policy receipt did not match the world")
    return p


def _layout_panes(tag: str) -> list[dict]:
    return [{"command": f"echo {tag}-A"}, {"command": f"echo {tag}-B"}]


def lp4() -> Proof:
    """Open a real layout, read both echo markers back, close it."""
    import shutil
    import time as _time

    from app.fabric import ghostty as g
    from app.fabric.terminal import GhosttyLayout, TmuxLayout, save_layout

    p = Proof("lp4", "Ghostty/tmux real layout")
    if shutil.which("tmux") is None:
        return p.owner("`brew install tmux`, then re-run `make live-proof`.",
                       "tmux is not on PATH")
    root = scratch_root()
    sock = "ev-liveproof"
    panes = _layout_panes("LP4")
    probed = g.probe().get("evidence") or {}
    use_ghostty = bool(probed.get("scriptable"))
    layout = GhosttyLayout(root=root, socket=sock) if use_ghostty else TmuxLayout(
        root=root, socket=sock)
    opened = layout.open(panes, confirm=True)
    if not opened.get("ok") and use_ghostty:
        # The dictionary exists but this window failed. Say so; do not pretend.
        layout = TmuxLayout(root=root, socket=sock)
        opened = layout.open(panes, confirm=True)
        use_ghostty = False
    if not opened.get("ok"):
        return p.fail(f"open: {_brief(opened)}")
    try:
        _time.sleep(1.8)
        read = layout.read_panes()
        blob = " ".join((read.get("evidence") or {}).get("panes", {}).values())
        saved = save_layout("lp4-live", {"panes": panes}, root=root)
        lines = [
            f"backend: {'ghostty+tmux' if use_ghostty else 'tmux-only'}",
            f"open: {opened.get('ok')} sess: {(opened.get('evidence') or {}).get('session')}",
            f"readback-a: {'LP4-A' in blob} | readback-b: {'LP4-B' in blob}",
            f"save-spec: {saved.get('ok')}",
        ]
        p.block("\n".join(lines))
        p.note("Socket ev-liveproof is a private tmux server. The owner's default "
               "server is not killed.")
        if "LP4-A" not in blob or "LP4-B" not in blob or not saved.get("ok"):
            p.fail(f"read-back missed a marker: {blob[:300]!r}")
    finally:
        layout.close()
        _run(["tmux", "-L", sock, "kill-server"])
    return p


def lp5() -> Proof:
    """Kill the proof tmux server and restore the saved spec."""
    import shutil
    import time as _time

    from app.fabric import terminal as term

    p = Proof("lp5", "layout restore from saved spec")
    if shutil.which("tmux") is None:
        return p.owner("`brew install tmux`, then re-run `make live-proof`.",
                       "tmux is not on PATH")
    root = scratch_root()
    sock = "ev-liveproof"
    panes = _layout_panes("LP5")
    saved = term.save_layout("lp5-live", {"panes": panes}, root=root)
    if not saved.get("ok"):
        return p.fail(f"save: {_brief(saved)}")
    first = term.TmuxLayout(root=root, socket=sock)
    opened = first.open(panes, confirm=True)
    if not opened.get("ok"):
        return p.fail(f"open: {_brief(opened)}")
    try:
        _run(["tmux", "-L", sock, "kill-server"])
        loaded = term.load_layout("lp5-live", root=root)
        second = term.TmuxLayout(root=root, socket=sock)
        reopened = second.open((loaded.get("spec") or {}).get("panes") or [], confirm=True)
        _time.sleep(1.8)
        blob = ""
        if reopened.get("ok"):
            blob = " ".join((second.read_panes().get("evidence") or {}).get("panes", {}).values())
        lines = [
            "server-killed: ev-liveproof (isolated socket, not the default server)",
            f"loaded: {loaded.get('ok')}",
            f"reopen: {reopened.get('ok')}",
            f"restored-a: {'LP5-A' in blob} | restored-b: {'LP5-B' in blob}",
        ]
        p.block("\n".join(lines))
        if "LP5-A" not in blob or "LP5-B" not in blob:
            p.fail(f"restore read-back missed a marker: {blob[:300]!r}")
        second.close()
    finally:
        _run(["tmux", "-L", sock, "kill-server"])
    return p


def lp6() -> Proof:
    """Visible layout, bridge capture, OCR artifact. Permission gaps stay honest."""
    import shutil
    import time as _time

    from app.fabric.bridge import Bridge
    from app.fabric.terminal import TmuxLayout

    p = Proof("lp6", "screenshot-to-layout loop")
    if shutil.which("tmux") is None:
        return p.owner("`brew install tmux`, then re-run `make live-proof`.",
                       "tmux is not on PATH")
    if not ensure_evie():
        return p.owner(EVIE_FIX, "Evie.app bridge did not answer")
    root = scratch_root()
    sock = "ev-liveproof"
    panes = _layout_panes("LP6")
    layout = TmuxLayout(root=root, socket=sock)
    opened = layout.open(panes, confirm=True)
    if not opened.get("ok"):
        return p.fail(f"open: {_brief(opened)}")
    try:
        _time.sleep(1.5)
        read = layout.read_panes()
        blob = " ".join((read.get("evidence") or {}).get("panes", {}).values())
        shot = Bridge().screenshot()
        if not shot.get("ok"):
            err = str(shot.get("error") or "capture failed")
            p.block(f"open: True\nmarkers: {'LP6-A' in blob and 'LP6-B' in blob}\n"
                    f"capture: {err}")
            return p.owner(SCREEN_FIX, err)
        path = str((shot.get("evidence") or {}).get("path") or "")

        async def _analyze():
            from app.fabric.screen_pipeline import analyze_image
            async with scratch_session() as session:
                return await analyze_image(path, session=session)

        art = asyncio.run(_analyze())
        artifact = (art.get("evidence") or {}).get("artifact") or {}
        lines = [
            f"open: {opened.get('ok')}",
            f"markers: {'LP6-A' in blob and 'LP6-B' in blob}",
            f"capture: {shot.get('ok')} path-exists: {Path(path).is_file()}",
            f"artifact: method={artifact.get('method')} panes={len(artifact.get('panes') or [])} "
            f"conf={artifact.get('confidence')} untrusted={artifact.get('untrusted')} "
            f"event={(art.get('evidence') or {}).get('event_id')}",
        ]
        p.block("\n".join(lines))
        if not (art.get("ok") and artifact.get("untrusted") is True
                and (art.get("evidence") or {}).get("event_id")):
            p.fail(f"artifact: {_brief(art)}")
    finally:
        layout.close()
        _run(["tmux", "-L", sock, "kill-server"])
    return p


def lp7() -> Proof:
    """Scratch SQLite DML preview/commit plus a throwaway Postgres container."""
    from app.fabric.sql_lane import execute, pg_execute

    p = Proof("lp7", "scratch SQL DML preview/commit/rollback and DDL snapshot")
    root = scratch_root()
    db = root / "lp7.db"
    db.unlink(missing_ok=True)
    (root / "ddl_snapshot.db").unlink(missing_ok=True)
    target = str(db)
    # The lane snapshots a DDL target only when the file already exists.
    import sqlite3
    sqlite3.connect(target).close()
    ddl = execute("CREATE TABLE lp7 (a TEXT)", db_path=target, root=root, confirm=True)
    seed = execute("INSERT INTO lp7 VALUES ('a')", db_path=target, root=root, confirm=True)
    preview = execute("UPDATE lp7 SET a='b'", db_path=target, root=root, preview=True)
    still = execute("SELECT a FROM lp7", db_path=target, root=root)
    commit = execute("UPDATE lp7 SET a='b'", db_path=target, root=root, confirm=True)
    now_b = execute("SELECT a FROM lp7", db_path=target, root=root)
    snap = (ddl.get("undo") or {}).get("snapshot") or (root / "ddl_snapshot.db").exists()
    sql_lines = [
        f"ddl: {ddl.get('ok')}",
        f"seed: {seed.get('ok')}",
        f"preview: {preview.get('ok')} rolled_back={(preview.get('evidence') or {}).get('rolled_back')}",
        f"still-a: {(still.get('evidence') or {}).get('rows')}",
        f"commit: {commit.get('ok')}",
        f"now-b: {(now_b.get('evidence') or {}).get('rows')}",
        f"ddl-snapshot: {bool(snap)}",
    ]
    sqlite_ok = (ddl.get("ok") and seed.get("ok") and preview.get("verified")
                 and (still.get("evidence") or {}).get("rows") == [("a",)]
                 and commit.get("ok")
                 and (now_b.get("evidence") or {}).get("rows") == [("b",)])
    if not sqlite_ok:
        p.fail("sqlite read-back did not match preview/commit")
    pg_lines = []
    container = "ev-liveproof-pg"
    try:
        ready = pg_execute("SELECT 1", container=container, root=root)
        if not ready.get("ok"):
            err = str(ready.get("error") or "")
            # A missing daemon is the owner's step. Any other postgres miss is a failure,
            # and it must not replace an earlier sqlite failure.
            if p.verdict == PASS and ("docker not installed" in err or "docker" in err.lower()):
                p.owner("Install and start Docker, then re-run `make live-proof`.", err)
            elif p.verdict == PASS:
                p.fail(f"postgres: {err}")
            pg_lines.append(f"ensure: False {err}")
        else:
            pg_execute("DROP TABLE IF EXISTS lp7", container=container, root=root, confirm=True)
            pg_execute("CREATE TABLE lp7 (a TEXT)", container=container, root=root, confirm=True)
            prev = pg_execute("INSERT INTO lp7 VALUES ('x')", container=container,
                              root=root, preview=True)
            after_prev = pg_execute("SELECT count(*) FROM lp7", container=container, root=root)
            committed = pg_execute("INSERT INTO lp7 VALUES ('x')", container=container,
                                   root=root, confirm=True)
            after = pg_execute("SELECT a FROM lp7", container=container, root=root)
            pg_lines = [
                "ensure: True",
                f"preview: {prev.get('ok')} rolled_back={(prev.get('evidence') or {}).get('rolled_back')}",
                f"rows-after-preview: {(after_prev.get('evidence') or {}).get('rows')}",
                f"commit: {committed.get('ok')}",
                f"rows-after-commit: {(after.get('evidence') or {}).get('rows')}",
            ]
            pg_ok = (prev.get("verified") and committed.get("ok")
                     and (after.get("evidence") or {}).get("rows") == [("x",)])
            if not pg_ok and p.verdict == PASS:
                p.fail("postgres read-back did not match the commit")
    finally:
        _run(["docker", "rm", "-f", container])
    p.block("sqlite\n" + "\n".join(sql_lines) + "\npostgres\n" + "\n".join(pg_lines))
    p.note(f"SQLite file {db}. Postgres container {container} is removed after the proof.")
    db.unlink(missing_ok=True)
    return p


def lp10() -> Proof:
    """Toy calculator planned and applied by the builder lane. No provider call."""
    import sys

    from app.fabric import builder

    p = Proof("lp10", "Builder job (toy spec, internal loop)")
    root = scratch_root() / "lp10calc"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    planned = builder.build_plan(
        "toy calc", root=root, project_dir=".",
        steps=[{"name": "test", "cmd": f"{sys.executable} -m pytest -q"}])
    if not planned.get("ok"):
        return p.fail(f"plan: {_brief(planned)}")
    applied = builder.apply_plan(planned, root=root)
    summary = (applied.get("evidence") or {}).get("test_summary") or {}
    resumed = builder.resume_job(str(planned.get("job_id") or ""), root=root)
    lines = [
        f"plan: {planned.get('ok')} {planned.get('job_id')}",
        f"apply: {applied.get('ok')} state={(applied.get('evidence') or {}).get('state')}",
        f"tests: {summary}",
        f"resume: {(resumed.get('evidence') or {}).get('state')}",
    ]
    p.block("\n".join(lines))
    p.note("Internal loop only. No coding-agent CLI is invoked, so this does not spend a provider quota.")
    if not (applied.get("ok") and summary.get("passed") == 1 and summary.get("failed") == 0):
        p.fail(f"apply: {_brief(applied)}")
    return p


# --------------------------------------------------------------------------- #
# LP8 / LP9 — communications and personal data (WP8)
# --------------------------------------------------------------------------- #

EVIE_LIST = "Evie Live Proof"
EVIE_FOLDER = "Evie Live Proof"


def lp8() -> Proof:
    """Real EventKit reminder lifecycle in the 'Evie Live Proof' list."""
    p = Proof("lp8", "real EventKit reminder lifecycle in a throwaway list")
    if not ensure_evie():
        return p.owner(EVIE_FIX, "Evie.app bridge did not answer.")
    from app.fabric.bridge import Bridge

    b = Bridge()
    made: list[str] = []

    def call(label: str, fn, *a, **kw) -> dict:
        res = fn(*a, **kw)
        ok = bool(res.get("ok"))
        p.note(f"{label}: ok={ok} verified={res.get('verified')} "
               f"err={str(res.get('error'))[:80]}")
        return res

    try:
        created_list = call("list-create", b.ek_reminder_list_create, EVIE_LIST)
        if not created_list.get("ok"):
            return p.fail(f"could not create the proof list: {created_list.get('error')}")
        made.append("list")

        created = call("reminder-create", b.ek_reminder_create,
                       "LP8 probe reminder", EVIE_LIST, "live proof")
        if not created.get("ok"):
            return p.fail(f"create failed: {created.get('error')}")
        rid = created["evidence"]["id"]
        made.append("reminder")

        listed = call("reminder-list", b.ek_reminder_list, EVIE_LIST)
        seen = [r.get("id") for r in listed.get("evidence", {}).get("reminders", [])]
        p.note(f"read-back: created id present = {rid in seen}")
        if rid not in seen:
            return p.fail("created reminder not visible on read-back")

        done = call("reminder-complete", b.ek_reminder_complete, rid)
        if not done.get("ok"):
            return p.fail("complete failed")
        after = b.ek_reminder_list(EVIE_LIST)
        row: dict[str, Any] = next(
            (r for r in after.get("evidence", {}).get("reminders", [])
             if r.get("id") == rid), {})
        p.note(f"completed flag after read-back: {row.get('done')}")
        p.note(f"list name resolved on the row: {row.get('list')!r}")
        if row.get("done") is not True:
            return p.fail("completion not reflected on read-back")

        # This is the lane the kernel actually calls, policy included.
        from app.ev.tools import dispatch  # noqa: F401  (import check only)

        p.note("kernel path: pim.act/reminder_delete through run_fabric_tool")
        lane_res = asyncio.run(lane("pim_act", {"op": "reminder_delete", "id": rid,
                                                "backend": "eventkit"}))
        p.note(f"lane delete: ok={lane_res.get('ok')} backend="
               f"{(lane_res.get('evidence') or {}).get('backend')}")
        if not lane_res.get("ok"):
            return p.fail(f"lane delete failed: {lane_res.get('error')}")
        made.remove("reminder")
        left = b.ek_reminder_list(EVIE_LIST)
        ids = [r.get("id") for r in left.get("evidence", {}).get("reminders", [])]
        p.note(f"gone after lane delete: {rid not in ids}")
        if rid in ids:
            return p.fail("reminder still present after lane delete")
    finally:
        if "reminder" in made:
            with contextlib.suppress(Exception):
                b.ek_reminder_delete(rid)  # type: ignore[possibly-undefined]
        if "list" in made:
            delres = b.ek_reminder_list_delete(EVIE_LIST)
            p.note(f"cleanup: list deleted ok={delres.get('ok')}")
    return p


def lp9() -> Proof:
    """Real Notes lifecycle in a throwaway folder."""
    p = Proof("lp9", "real Notes lifecycle in a throwaway folder")
    if not ensure_evie():
        return p.owner(EVIE_FIX, "Evie.app bridge did not answer.")
    from app.fabric.bridge import Bridge

    b = Bridge()
    kept: list[str] = []

    def call(label: str, fn, *a, **kw) -> dict:
        res = fn(*a, **kw)
        p.note(f"{label}: ok={res.get('ok')} verified={res.get('verified')} "
               f"err={str(res.get('error'))[:90]}")
        return res

    try:
        before = call("folders", b.notes_folders)
        if not before.get("ok"):
            if str(before.get("error", "")).startswith("jxa_failed"):
                return p.owner(NOTES_FIX, str(before.get("error"))[:200])
            return p.fail(f"folder list failed: {before.get('error')}")

        created = call("note-create", b.note_create, EVIE_FOLDER,
                       "LP9 probe note", "written by make live-proof")
        if not created.get("ok"):
            return p.fail(f"create failed: {created.get('error')}")
        kept.append("note")
        nid = created["evidence"]["id"]

        listed = call("note-list", b.note_list, EVIE_FOLDER)
        ids = [n.get("id") for n in listed.get("evidence", {}).get("notes", [])]
        p.note(f"read-back: created id present = {nid in ids}")
        if nid not in ids:
            return p.fail("created note not visible on read-back")

        deleted = call("note-delete", b.note_delete, EVIE_FOLDER, nid)
        if not deleted.get("ok"):
            return p.fail(f"delete failed: {deleted.get('error')}")
        kept.remove("note")
        left = b.note_list(EVIE_FOLDER)
        ids = [n.get("id") for n in left.get("evidence", {}).get("notes", [])]
        p.note(f"gone after delete: {nid not in ids}")
        if nid in ids:
            return p.fail("note still present after delete")
    finally:
        if "note" in kept:
            with contextlib.suppress(Exception):
                b.note_delete(EVIE_FOLDER, nid)  # type: ignore[possibly-undefined]
    return p


# --------------------------------------------------------------------------- #
# LP11 — iPhone / PWA over real HTTP (WP11)
# --------------------------------------------------------------------------- #

LP11_PORT = 18001


def _tailnet_host() -> str:
    binary = shutil.which("tailscale") or "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    if not os.path.exists(binary):
        return "evie.local"
    out = _run([binary, "serve", "status"]).stdout or ""
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("https://") and "ts.net" in line:
            return line.split()[0].split("://", 1)[-1].split("/", 1)[0]
    return "evie.local"


def lp11() -> Proof:
    """Isolated API on :18001 + scratch DB: PWA, panel, pairing, phone turn."""
    import base64
    import secrets
    import socket

    import httpx

    p = Proof("lp11", "PWA + phone panel + pairing + typed turn over real HTTP")
    port = LP11_PORT
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", port)) == 0:
            return p.owner(
                f"Port {port} is busy; stop whatever owns it, then re-run.",
                f"another process is listening on {port}")
    scratch = scratch_root() / "lp11-scratch"
    with contextlib.suppress(OSError):
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    db = scratch / "lp11.db"
    master = "lp11-" + secrets.token_urlsafe(24)
    env = {
        **os.environ,
        "EV_MASTER_KEY": master,
        "EV_VAULT_KEY": base64.b64encode(secrets.token_bytes(48)).decode(),
        "EV_DATABASE_URL": f"sqlite+aiosqlite:///{db}",
        "EV_STORAGE_ROOT": str(scratch / "storage"),
        "EV_FABRIC": "1",
        "EV_ENV": "development",
    }
    backend = ROOT / "backend"
    log = scratch / "server.log"
    host = _tailnet_host()
    origin = f"https://{host}"
    headers = {"Origin": origin, "Host": host}
    proc = None
    try:
        with log.open("w") as fh:
            proc = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app",
                 "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
                cwd=str(backend), env=env, stdout=fh, stderr=subprocess.STDOUT)
        base = f"http://127.0.0.1:{port}"
        deadline = time.time() + 90
        ready = False
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                if httpx.get(f"{base}/v1/health", timeout=2.0).status_code == 200:
                    ready = True
                    break
            except Exception:
                pass
            time.sleep(1.0)
        if not ready:
            tail = log.read_text()[-800:] if log.exists() else ""
            return p.fail("isolated API did not become healthy\n" + tail)
        p.note(f"isolated API healthy on :{port} (scratch DB, EV_ENV=development)")

        with httpx.Client(base_url=base, headers=headers, timeout=20.0) as client:
            index = client.get("/evie")
            p.note(f"GET /evie -> {index.status_code}")
            if index.status_code != 200 or "Evie" not in index.text:
                return p.fail(f"PWA index not served (HTTP {index.status_code})")
            if "/evie/evie-phone.js" not in index.text:
                return p.fail("PWA index does not load the phone panel")
            panel = client.get("/evie/evie-phone.js")
            disk = (backend / "clients" / "pwa" / "evie-phone.js").read_text()
            p.note(f"GET /evie/evie-phone.js -> {panel.status_code}, "
                   f"bytes match tree = {panel.text == disk}")
            if panel.status_code != 200 or panel.text != disk:
                return p.fail("panel asset served differs from the tree")

            minted = client.post("/v1/device-gateway/pairing-tokens",
                                 headers={**headers, "Authorization": f"Bearer {master}"},
                                 json={"role": "primary_companion",
                                       "display_name": "LP11 phone"})
            p.note(f"POST pairing-tokens -> {minted.status_code}")
            if minted.status_code != 200:
                return p.fail(f"pairing token failed: {minted.text[:200]}")
            code = minted.json().get("pairing_token") or ""

            paired = client.post("/v1/device-gateway/pair", json={
                "pairing_token": code, "display_name": "LP11 phone",
                "platform": "ios", "capabilities": ["text", "camera"],
                "client_version": "lp11", "instance_id": "lp11",
            })
            p.note(f"POST /pair -> {paired.status_code}; "
                   f"device token issued = {bool(paired.json().get('device_token')) if paired.status_code == 200 else False}")
            if paired.status_code != 200:
                return p.fail(f"pairing failed: {paired.text[:200]}")
            device_token = paired.json().get("device_token") or ""
            auth = {**headers, "Authorization": f"Bearer {device_token}"}

            status = client.get("/v1/fabric/status", headers=auth)
            p.note(f"GET /v1/fabric/status (device token) -> {status.status_code}; "
                   f"fabric_enabled={status.json().get('fabric_enabled') if status.status_code == 200 else 'n/a'}")
            pending = client.get("/v1/fabric/approvals/pending", headers=auth)
            p.note(f"GET /v1/fabric/approvals/pending -> {pending.status_code}; "
                   f"count={pending.json().get('count') if pending.status_code == 200 else 'n/a'}")

            turn = client.post("/v1/device-gateway/text", headers=auth, json={
                "text": "status", "instance_id": "lp11", "request_id": "lp11-turn-1",
            })
            body = turn.json() if turn.status_code == 200 else {}
            reply = str(body.get("reply") or body.get("spoken") or "")
            p.note(f"POST /v1/device-gateway/text -> {turn.status_code}; "
                   f"route={body.get('route')}; reply chars={len(reply)}; "
                   f"ok={body.get('ok')}")
            if turn.status_code != 200:
                return p.fail(f"typed turn failed: {turn.text[:200]}")
            if not reply:
                return p.fail("typed turn returned no reply")

            stop = client.post("/v1/fabric/cancel", headers=auth, json={"turn_id": None})
            p.note(f"POST /v1/fabric/cancel -> {stop.status_code}; "
                   f"ok={stop.json().get('ok') if stop.status_code == 200 else 'n/a'}")
            if stop.status_code != 200:
                return p.fail(f"cancel endpoint failed: {stop.text[:200]}")
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            with contextlib.suppress(Exception):
                proc.wait(timeout=15)
        with contextlib.suppress(OSError):
            for leftover in scratch.glob("lp11.db*"):
                leftover.unlink()
    return p


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #

PROOFS: dict[str, Callable[[], Proof]] = {
    "lp1": lp1,
    "lp2": lp2,
    "lp3": lp3,
    "lp4": lp4,
    "lp5": lp5,
    "lp6": lp6,
    "lp7": lp7,
    "lp8": lp8,
    "lp9": lp9,
    "lp10": lp10,
    "lp11": lp11,
}


def run(only: list[str] | None = None) -> list[Proof]:
    names = only or list(PROOFS)
    out: list[Proof] = []
    for name in names:
        fn = PROOFS.get(name)
        if fn is None:
            continue
        print(f"[live-proof] {name} ...", flush=True)
        t0 = time.time()
        try:
            proof = fn()
        except Exception as exc:  # a crashing proof is a FAIL, never a PASS
            proof = Proof(name, "crashed")
            proof.fail(f"{type(exc).__name__}: {exc}")
        proof.note(f"duration: {time.time() - t0:.1f}s")
        print(f"[live-proof] {name} -> {proof.verdict}", flush=True)
        out.append(proof)
    return out


def write_doc(proofs: list[Proof]) -> None:
    """Regenerate the LP sections only, keeping the header and any LP we did not run."""
    existing = DOC.read_text() if DOC.exists() else ""
    head = existing.split("\n## ", 1)[0].rstrip() + "\n"
    if not existing:
        head = ("# Live Proofs (evie-fabric final run)\n\n"
                "Generated by `make live-proof`.\n")
    sections: dict[str, str] = {}
    for chunk in existing.split("\n## ")[1:]:
        key = chunk.split(" ", 1)[0].strip().lower()
        sections[key] = "## " + chunk
    for proof in proofs:
        sections[proof.lp] = proof.render()
    def _ordinal(key: str) -> int:
        tail = key[2:]
        return int(tail) if tail.isdigit() else 999

    order = sorted(sections, key=_ordinal)
    body = "\n".join(sections[k].rstrip() + "\n" for k in order if k in sections)
    DOC.write_text(head + "\n" + body)
    print(f"[live-proof] wrote {DOC}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="live-proof")
    ap.add_argument("--only", default="", help="comma list, e.g. lp8,lp9")
    ap.add_argument("--stdout", action="store_true", help="print, do not write")
    ap.add_argument("--json", default="", help="also write a machine summary here")
    args = ap.parse_args(argv)

    only = [x.strip().lower() for x in args.only.split(",") if x.strip()]
    proofs = run(only or None)
    if args.stdout:
        for p in proofs:
            print("\n" + p.render())
    else:
        write_doc(proofs)
    if args.json:
        Path(args.json).write_text(json.dumps(
            [{"lp": p.lp, "verdict": p.verdict, "detail": p.detail,
              "owner_fix": p.owner_fix, "stamp": p.stamp} for p in proofs], indent=1))
    bad = [p for p in proofs if p.verdict == FAIL]
    print(f"[live-proof] {len(proofs)} proofs: "
          f"{sum(1 for p in proofs if p.verdict == PASS)} PASS, "
          f"{sum(1 for p in proofs if p.verdict == OWNER)} OWNER-ACTION, "
          f"{len(bad)} FAIL")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
