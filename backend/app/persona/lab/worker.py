"""Lab worker: one brain turn for one variant (subprocess per variant).

The flag is resolved at import for the v2.1 constant, so each variant runs
in its own process: v4 boots with the flag off (default), v2.1 sets it
before importing anything under app.ev. Usage:

  python -m app.persona.lab.worker '<json payload>'   # prints JSON reply
  python -m app.persona.lab.worker --selftest          # no brain: systems only

Payload: {variant, owner_msg, now:{local_time,session_started,task,
last_result,extra}, history:[[role,text]...], temperature, lab_line,
thread_id, turn_index}
"""

from __future__ import annotations

import json
import sys


def _build_v4(owner_msg: str, now: dict) -> str:
    from app.ev.dynamic_personality import turn_guidance
    from app.ev.personality import SPEECH_STYLE_INSTRUCTIONS, identity_block

    parts = [
        identity_block("EVIE", "the owner's personal AI", compact=True),
        turn_guidance(owner_msg),
    ]
    facts = " ".join(str(now.get(key, "")) for key in
                     ("local_time", "session", "task", "last_result", "extra")).strip()
    if facts:
        parts.append(f"Context: {facts}")
    parts.append(SPEECH_STYLE_INSTRUCTIONS)
    return "\n\n".join(parts)


def _build_v21(owner_msg: str, now: dict, thread_id: str, turn_index: int):
    from datetime import datetime

    from app.persona.compile import build_persona_block
    from app.persona.context import PersonaContext, WorkState

    def dt(value):
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    ctx = PersonaContext(
        channel="text",
        owner_msg=owner_msg,
        local_time=dt(now.get("local_time")),
        session_started=dt(now.get("session_started")),
        work_state=WorkState(task=now.get("task"), last_result=now.get("last_result")),
        thread_id=thread_id,
        turn_index=turn_index,
    )
    return build_persona_block(ctx)


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[1] == "--selftest":
        from app.persona.core import core_only

        v4sys = _build_v4("hello", {})
        print(json.dumps({"v4_chars": len(v4sys), "core_chars": len(core_only())}))
        return 0
    payload = json.loads(argv[1] if len(argv) > 1 else sys.stdin.read())
    variant = payload.get("variant", "v2.1")
    if variant == "v2.1":
        from app.persona import settings

        settings.PERSONA_ENABLED = True
    owner_msg = payload.get("owner_msg", "")
    now = payload.get("now", {}) or {}
    history = payload.get("history", []) or []
    temperature = float(payload.get("temperature", 0.7))
    thread_id = payload.get("thread_id", "lab")
    turn_index = int(payload.get("turn_index", 0))

    if variant == "v2.1":
        block = _build_v21(owner_msg, now, thread_id, turn_index)
        system = block.text
        tone: dict = {
            "roast": block.tone.roast, "technique": block.tone.technique,
            "shape": block.tone.shape, "profanity": block.tone.profanity,
            "audience": block.tone.audience, "reasons": list(block.tone.reasons),
        }
        tokens = block.token_estimate
    else:
        system = _build_v4(owner_msg, now)
        tone, tokens = {"variant": "v4"}, len(system) // 4
    if payload.get("lab_line", True):
        from app.persona.lab.brain import LAB_LINE

        system += "\n\n" + LAB_LINE
    if payload.get("dry", False):
        print(json.dumps({"system": system, "tone": tone, "tokens": tokens}))
        return 0
    from app.persona.lab.brain import call_brain

    reply, usage = call_brain(system, owner_msg, history, temperature=temperature)
    print(json.dumps({"reply": reply, "tone": tone, "tokens": tokens,
                      "usage": usage}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
