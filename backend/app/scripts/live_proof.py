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
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[3]
DOC = ROOT / "docs" / "LIVE_PROOF.md"
PROOF_ROOT = Path.home() / "Desktop" / "evie-live-proof"

PASS, FAIL, OWNER = "PASS", "FAIL", "OWNER-ACTION"


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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

    def owner(self, fix: str, detail: str = "") -> "Proof":
        self.verdict = OWNER
        self.owner_fix = fix
        self.detail = detail
        return self

    def fail(self, detail: str) -> "Proof":
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
        row = next((r for r in after.get("evidence", {}).get("reminders", [])
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
# registry
# --------------------------------------------------------------------------- #

PROOFS: dict[str, Callable[[], Proof]] = {
    "lp8": lp8,
    "lp9": lp9,
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
    order = [f"lp{i}" for i in range(1, 11)]
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
