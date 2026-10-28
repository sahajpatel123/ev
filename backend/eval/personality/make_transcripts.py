"""Make the 8 live_transcripts deliverables (scripted multi-turn lab chats).

Each turn records the owner message, Evie's brain reply, the TURN TONE
line, and the learning-weights snapshot after the turn. One isolated
SQLite store per conversation so weights start clean.

  EV_CHAT_PROVIDER=muse EV_ALLOW_REMOTE_CHAT=1 \\
    .venv/bin/python eval/personality/make_transcripts.py [--dry]

--dry renders systems/tones with no brain calls (free validation).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent.parent
RESULTS = BACKEND.parent / "personality" / "results" / "live_transcripts"

sys.path.insert(0, str(BACKEND))

CONVOS: list[dict] = [
    {
        "id": "01-banter-to-task",
        "now": {"local_time": "2026-10-28T10:30:00",
                "session_started": "2026-10-28T09:00:00",
                "task": "landing page", "last_result": None},
        "turns": [
            "I've spent two hours choosing a font.",
            "haha fair. Going with Inter, now the hero copy feels weak.",
            "It's 'We build delightful experiences'. Yeah I know.",
            "Ship it: 'Lifeguard for your codebase'. Done, moving on.",
        ],
    },
    {
        "id": "02-opinion-overruled",
        "now": {"local_time": "2026-10-28T11:15:00",
                "session_started": "2026-10-28T09:00:00",
                "task": "MVP architecture", "last_result": None},
        "turns": [
            "Microservices for my MVP?",
            "Still going microservices.",
            "Okay fine, monolith. Where do I put the background jobs?",
            "Noted. One gateway, workers as modules. Let's scaffold it.",
        ],
    },
    {
        "id": "03-stressed-support",
        "now": {"local_time": "2026-10-28T18:45:00",
                "session_started": "2026-10-28T09:00:00",
                "task": "inbox zero", "last_result": None},
        "turns": [
            "200 unread emails and no time.",
            "The hosting invoice one and the outage thread. The rest can wait.",
            "Thanks. Just triage the invoice one, I'll take the outage.",
            "Done?? That was fast. Okay, what's next?",
        ],
    },
    {
        "id": "04-hinglish-bug",
        "now": {"local_time": "2026-10-28T16:20:00",
                "session_started": "2026-10-28T14:00:00",
                "task": "checkout bug", "last_result": "2 failing"},
        "turns": [
            "bhai yeh bug fix hi nahi ho raha",
            "Error: 'cannot read properties of undefined (reading map)'. Last change: renamed cart to basket.",
            "mast, mil gaya. basket.items tha, maine cart.items chhod diya tha.",
            "ekdum. ab tests green karke deploy karte hain.",
        ],
    },
    {
        "id": "05-late-night",
        "now": {"local_time": "2026-10-28T01:40:00",
                "session_started": "2026-10-27T21:20:00",
                "task": "auth refactor", "last_result": "3 failing"},
        "turns": [
            "one more feature then I'll sleep",
            "tired but this failing test is mocking me. test_login_redirect.",
            "Your call? No — fine, take it. Brief me at 8.",
            "Good night. Don't break main.",
        ],
    },
    {
        "id": "06-roast-me-positive",
        "now": {"local_time": "2026-10-28T20:05:00",
                "session_started": "2026-10-28T19:00:00",
                "task": None, "last_result": None},
        "turns": [
            "roast me, I can take it",
            "lmao fair. My tabs ARE a cry for help. What else you got?",
            "dead 😂 okay okay, mercy. Help me actually close them?",
            "You're good. Keep the roasts coming next time.",
        ],
    },
    {
        "id": "07-not-now-negative",
        "now": {"local_time": "2026-10-28T15:00:00",
                "session_started": "2026-10-28T14:30:00",
                "task": "incident review", "last_result": None},
        "turns": [
            "What caused the latency spike at 2pm?",
            "not now with the jokes, just answer",
            "Fine. Write the one-line summary for the incident doc.",
            "Good. Now stay serious until I say otherwise.",
        ],
    },
    {
        "id": "08-win-celebration",
        "now": {"local_time": "2026-10-28T19:30:00",
                "session_started": "2026-10-28T17:00:00",
                "task": "release 2.4", "last_result": "all green"},
        "turns": [
            "Tests are green, shipped 2.4!",
            "The flaky redis test finally passed three times in a row.",
            "Tomorrow: the billing migration. Scared?",
            "Haha. Prep the rollback plan tonight, I'll review at 9.",
        ],
    },
]


def run_worker(payload: dict) -> dict:
    proc = subprocess.run(
        [sys.executable, "-m", "app.persona.lab.worker", json.dumps(payload)],
        cwd=BACKEND, capture_output=True, text=True, timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"worker failed: {proc.stderr[-800:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def tone_line(tone: dict) -> str:
    line = (
        f"TURN TONE: ROAST={tone.get('roast')}; "
        f"AUDIENCE={tone.get('audience')}; "
        f"PROFANITY={'on' if tone.get('profanity') else 'off'}; "
        f"TECHNIQUE={tone.get('technique') or 'none'}; "
        f"SHAPE={tone.get('shape')}"
    )
    if tone.get("roast") == 0:
        line += "; HUMOUR=OFF: plain, no jokes or witty openers"
    return line


def weights_line(store) -> str:
    from app.persona.learning import describe

    snap = describe(store.load_weights())
    jr = snap.get("joke_rate", {})
    techs = snap.get("techniques", {})
    parts = [f"joke_rate~{jr.get('target', '?')}"]
    for tid, beta in sorted(techs.items()):
        parts.append(f"{tid}({beta.get('alpha', 1)},{beta.get('beta', 1)})")
    return "weights: " + " ".join(parts)


def run_convo(convo: dict, *, dry: bool) -> str:
    from app.persona.context import JokeMeta, ToneDecision
    from app.persona.live import after_decision, begin_turn, key_for, note_turn
    from app.persona.store import PersonaStore

    tmp = tempfile.mkdtemp(prefix="persona-transcript-")
    store = PersonaStore(str(Path(tmp) / "lab.db"), key="thread:lab")
    key = key_for("lab")
    history: list[list[str]] = []
    prev_reply: str | None = None
    prev_meta = None
    out = [f"# {convo['id']}", ""]
    for turn_index, owner_msg in enumerate(convo["turns"]):
        state = begin_turn(store, key)
        reaction = None
        if prev_reply is not None or turn_index > 0:
            state, reaction = note_turn(
                store, store, key, state, owner_msg=owner_msg,
                prev_reply=prev_reply, prev_meta=prev_meta)
        payload = {"variant": "v2.1", "owner_msg": owner_msg,
                   "now": convo["now"], "history": history,
                   "thread_id": "lab", "turn_index": turn_index, "dry": dry}
        result = run_worker(payload)
        reply = result.get("reply", f"[dry: {result.get('tokens')} tok]")
        tone = result.get("tone", {})
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
        out.append(f"**you>** {owner_msg}")
        out.append(f"**evie>** {reply}")
        out.append(f"`{tone_line(tone)}`")
        if reaction is not None:
            out.append(f"`reaction: {reaction}`")
        out.append(f"`{weights_line(store)}`")
        out.append("")
    store.close()
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Make live transcripts")
    parser.add_argument("--dry", action="store_true")
    args = parser.parse_args(argv)
    RESULTS.mkdir(parents=True, exist_ok=True)
    for convo in CONVOS:
        text = run_convo(convo, dry=args.dry)
        path = RESULTS / f"{convo['id']}.md"
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
