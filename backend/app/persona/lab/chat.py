"""Persona lab: chat with v2.1 Evie on the real brain (tools never execute).

  python -m app.persona.lab.chat [--compare] [--debug] [--night]

NOW signals are real and local: owner time, session length, git branch +
dirty count of this repo, last pytest result when a recent cache exists.
Learning/latch persist in the lab store (~/.ev/persona/lab.db). piped
stdin works (EOF ends the session with a summary).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

os.environ.setdefault("EV_PERSONA_STORE", str(Path("~/.ev/persona/lab.db").expanduser()))

BACKEND = Path(__file__).resolve().parent.parent.parent.parent


def _git_branch_dirty() -> tuple[str, int]:
    try:
        branch = subprocess.run(
            ["git", "branch", "--show-current"], cwd=BACKEND.parent,
            capture_output=True, text=True, timeout=5,
        ).stdout.strip() or "?"
        dirty = subprocess.run(
            ["git", "status", "--porcelain=v1"], cwd=BACKEND.parent,
            capture_output=True, text=True, timeout=5,
        ).stdout.strip().splitlines()
        count = len([line for line in dirty if line.strip()])
        return branch, count
    except Exception:
        return "?", 0


def _pytest_last() -> str | None:
    try:
        cache = BACKEND / ".pytest_cache" / "v" / "cache"
        lastfailed = cache / "lastfailed"
        if lastfailed.exists():
            age_h = (datetime.now().timestamp() - lastfailed.stat().st_mtime) / 3600
            if age_h > 24:
                return None
            failed = json.loads(lastfailed.read_text(encoding="utf-8") or "{}")
            if failed:
                return f"{len(failed)} failing"
            return "green"
        return None
    except Exception:
        return None


def run_worker(payload: dict) -> dict:
    """One worker subprocess per variant call (flag resolved at import)."""
    env = dict(os.environ)
    proc = subprocess.run(
        [sys.executable, "-m", "app.persona.lab.worker", json.dumps(payload)],
        cwd=BACKEND, capture_output=True, text=True, timeout=180, env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"worker failed: {proc.stderr[-1500:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Chat with v2.1 Evie (lab)")
    parser.add_argument("--compare", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--night", action="store_true")
    parser.add_argument("--store", default=None)
    args = parser.parse_args(argv)
    if args.store:
        os.environ["EV_PERSONA_STORE"] = args.store

    from app.persona.live import after_decision, begin_turn, key_for, note_turn
    from app.persona.store import PersonaStore
    from app.persona.tone import ToneDecision

    store = PersonaStore(os.environ["EV_PERSONA_STORE"], key="thread:lab")
    ledger = store
    key = key_for("lab")
    history: list[list[str]] = []
    turn_index = 0
    session_start = datetime.now()
    prev_reply: str | None = None
    prev_meta = None
    night_state = None
    print("lab chat (v2.1). EOF (Ctrl-D) to end.", flush=True)

    while True:
        try:
            owner_msg = input("you> ").strip()
        except EOFError:
            print("\n[lab session ended]")
            break
        if not owner_msg:
            continue
        state = begin_turn(store, key)
        if prev_reply is not None or turn_index > 0:
            state, reaction = note_turn(
                store, ledger, key, state, owner_msg=owner_msg,
                prev_reply=prev_reply, prev_meta=prev_meta)
        now: dict[str, Any]
        if args.night:
            now = {"local_time": "2026-10-28T02:30:00",
                   "session_started": "2026-10-27T21:20:00",
                   "task": "auth refactor", "last_result": "3 failing"}
        else:
            branch, dirty = _git_branch_dirty()
            last = _pytest_last()
            now = {"local_time": datetime.now().isoformat(timespec="seconds"),
                   "session_started": session_start.isoformat(timespec="seconds"),
                   "task": f"{branch} ({dirty} dirty)" if branch != "?" else None,
                   "last_result": last}
        payload = {"owner_msg": owner_msg, "now": now, "history": history,
                   "thread_id": "lab", "turn_index": turn_index}
        v21 = run_worker({**payload, "variant": "v2.1"})
        reply = v21["reply"]
        tone = v21.get("tone", {})
        print(f"evie> {reply}", flush=True)
        if args.compare:
            v4 = run_worker({**payload, "variant": "v4"})
            print(f"v4  > {v4['reply']}", flush=True)
        if args.debug:
            print(f"[tone] roast={tone.get('roast')} tech={tone.get('technique')} "
                  f"shape={tone.get('shape')} reasons={tone.get('reasons')}", flush=True)
        if args.night:
            from app.persona.context import PersonaContext, WorkState
            from app.persona.nightshift import (
                plan_from_context,
                should_nudge,
                validate,
            )

            nctx = PersonaContext(
                channel="text", owner_msg=owner_msg,
                local_time=datetime(2026, 10, 28, 2, 30),
                session_started=datetime(2026, 10, 27, 21, 20),
                work_state=WorkState(task="auth refactor",
                                     last_result="3 failing"))
            if should_nudge(nctx, night_state, recent_owner_msgs=[owner_msg]):
                plan = plan_from_context(nctx, allow_execution=True)
                assert plan is not None and validate(plan) == ()
                print(f"[night] offer: sleep — I'll take auth refactor "
                      f"({plan.workspace}); brief by morning", flush=True)
                if owner_msg.lower().strip() in ("go", "yes take it", "fine, take it",
                                                 "fine take it", "do it"):
                    print("[night] plan validated; sign-off: phone stays quiet "
                          "unless something's on fire.", flush=True)
        from app.persona.context import JokeMeta

        decision = ToneDecision(
            roast=int(tone.get("roast", 1)), humour_allowed=True,
            technique=tone.get("technique"), profanity=False,
            audience="private", reasons=tuple(tone.get("reasons", [])))
        state = after_decision(store, key, state, decision, reply,
                               owner_msg=owner_msg)
        prev_meta = JokeMeta(technique=tone.get("technique"), topic=None)
        prev_reply = reply
        history.append(["user", owner_msg])
        history.append(["assistant", reply])
        history = history[-12:]
        turn_index += 1
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
