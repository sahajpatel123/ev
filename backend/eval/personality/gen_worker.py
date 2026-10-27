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
import asyncio
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
    from app.contracts import ChatMessage
    from app.ev.assistant import spoken_name
    from app.ev.personality import identity_block
    from app.ev.turn import (
        ActionReceipt,
        build_system_prompt,
        receipt_messages,
    )
    from app.gateway.providers import get_chat_provider
    from app.gateway.service import ModelGateway

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
    call = asyncio.run(
        ModelGateway(provider).chat(
            [
                ChatMessage(role="system", content=system_prompt),
                ChatMessage(role="user", content=scenario.get("owner_msg", "")),
                *receipt_messages(receipts),
            ],
            temperature=args.temperature,
        )
    )
    if call.status != "ok":
        raise RuntimeError(f"brain call failed: {call.error}")
    usage = call.usage() if callable(call.usage) else (call.usage or {})
    json.dump({"reply": call.result.text, "usage": dict(usage or {})}, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
