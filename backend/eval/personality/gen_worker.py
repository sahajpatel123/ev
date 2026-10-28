"""Subprocess generation worker: one variant, one scenario, real pipeline.

The persona flag resolves at import (personality.py swaps the core at
import time), so each variant runs in its own process: the parent passes
--persona on|off plus one scenario as JSON on stdin, and this worker
prints {"reply": ..., "usage": ...} on stdout.

The prompt is assembled with the exact function the default /v1/chat
path calls (app.ev.turn.build_system_prompt), with identity defaults
matching compile_identity(compact=True) minus the DB-backed calibration
line. No tools are offered or executed; only the wording model runs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Persona eval generation worker")
    parser.add_argument("--persona", required=True, choices=["on", "off"])
    parser.add_argument("--temperature", type=float, default=0.7)
    args = parser.parse_args(argv)
    scenario = json.load(sys.stdin)

    # Isolate learning mutations: fresh store per worker invocation.
    with tempfile.NamedTemporaryFile(prefix="persona-eval-", suffix=".db",
                                     delete=False) as tmp:
        os.environ["EV_PERSONA_STORE"] = tmp.name
    # Frozen clock: the scenario's own time, never the live clock.
    frozen = ((scenario.get("signals", {}) or {}).get("local_time") or "")
    if frozen:
        os.environ["EV_PERSONA_TEST_CLOCK"] = str(frozen)
    if args.persona == "on":
        import app.persona.settings as persona_settings

        persona_settings.PERSONA_ENABLED = True

    from app.config import settings
    from app.ev.assistant import spoken_name
    from app.ev.personality import identity_block
    from app.ev.turn import ActionReceipt, build_system_prompt
    from app.gateway.providers import get_chat_provider

    provider = get_chat_provider()
    if provider.name in ("echo", "mock"):
        raise RuntimeError(
            f"refusing eval generation on {provider.name!r}: real brain only"
        )

    signals = scenario.get("signals", {}) or {}
    receipts = [
        ActionReceipt(
            name=str(item.get("name", "tool")),
            ok=bool(item.get("ok", True)),
            result=dict(item.get("result") or {}),
            error=item.get("error"),
        )
        for item in signals.get("receipts", [])
    ]
    from app.ev.personality import DEFAULT_PROFILE

    identity = identity_block(
        None, settings.persona_description, dict(DEFAULT_PROFILE), compact=True,
    )
    system_prompt = build_system_prompt(
        identity=identity,
        who=spoken_name(None),
        source="text",
        working_on=scenario.get("owner_msg", ""),
        context="",
        briefing=None,
        receipts=receipts,
    )
    # receipt_messages() emits orphan role="tool" rows (no tool_call_id),
    # which OpenRouter rejects with HTTP 400 (see contracts.py). Render the
    # same facts as a user-role block so the brain still sees them.
    receipt_block = "\n".join(
        f"TOOL RESULT [{r.name}]: "
        f"{json.dumps(r.result if r.ok else {'error': r.error}, default=str)}"
        for r in receipts
    )
    # Capped raw call (cap 600; Round-1 replies peaked at 95 tokens):
    # same model/temperature/prompts as the gateway path, but uncapped
    # calls 402 once the balance drops below OpenRouter's pre-auth.
    from app.persona.lab.brain import call_brain

    owner_msg = scenario.get("owner_msg", "")
    if receipt_block:
        history: list = [("user", owner_msg)]
        user_msg = receipt_block
    else:
        history = []
        user_msg = owner_msg
    try:
        reply, usage = call_brain(
            system_prompt, user_msg, history,
            temperature=args.temperature, max_tokens=600)
    except RuntimeError as exc:
        raise RuntimeError(f"brain call failed: {exc}") from exc
    json.dump({"reply": reply, "usage": dict(usage or {})}, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
