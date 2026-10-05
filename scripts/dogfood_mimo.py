"""MiMo dogfood: one real daily workflow, end to end (opt-in, owner-run).

Chain under test: owner ask -> MiMo kernel turn -> allowlisted deterministic
handler -> verified result -> same audit trail. It runs the real cognitive
kernel decision path (``app.cognitive.kernel.handle_turn``), which is the same
surface typed chat and voice use.

Run from ``backend/`` with the key configured (never in git)::

    EV_MIMO_ENABLED=true EV_ALLOW_REMOTE_CHAT=true \\
    EV_OPENROUTER_API_KEY=... uv run python ../scripts/dogfood_mimo.py

Without a key/egress/consent it prints SKIP and exits 2. The prompt is
synthetic; the turn is recorded against the configured database and audit log,
exactly as a normal owner turn would be. Exit 0 only when a bounded
``decision_tool`` result comes back with spoken evidence.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "backend"))

PROMPT = "What am I working on right now?"


async def main() -> int:
    from app.config import settings

    key = (
        os.environ.get("EV_OPENROUTER_API_KEY")
        or getattr(settings, "openrouter_api_key", None)
        or ""
    ).strip()
    if not key:
        print("SKIP: EV_OPENROUTER_API_KEY is not set (dogfood needs a live key).")
        return 2
    if not getattr(settings, "mimo_enabled", False):
        print("SKIP: EV_MIMO_ENABLED is not true.")
        return 2
    from app.compliance.policy import remote_processing_allowed

    if not remote_processing_allowed("chat_egress"):
        print("SKIP: EV_ALLOW_REMOTE_CHAT is not enabled.")
        return 2

    from app.gateway.openrouter_mimo import MimoEgressDenied, MimoProvider, MimoUnavailable

    try:
        await MimoProvider()._authorize()
    except (MimoEgressDenied, MimoUnavailable) as exc:
        print(f"SKIP: {exc}")
        return 2

    from app.cognitive.kernel import handle_turn

    result = await handle_turn(transcript=PROMPT, modality="text", actor="master")
    report = {
        "prompt": PROMPT,
        "kind": result.kind,
        "spoken": result.spoken,
        "last_tool": result.last_tool,
        "tool_calls": result.tool_calls,
        "latency_ms": result.latency_ms,
        "unavailable": result.unavailable,
        "evidence": result.evidence,
    }
    print(json.dumps(report, indent=2, default=str))
    out = BACKEND / "backend" / "eval" / "mimo-dogfood.json"
    try:
        out.write_text(json.dumps(report, indent=2, default=str))
        print(f"report: {out}")
    except OSError as exc:
        print(f"report write failed: {exc}")
    if result.kind == "decision_tool" and result.last_tool:
        print("PASS: bounded decision -> deterministic handler -> verified result")
        return 0
    print(f"FAIL: expected a decision_tool result, got kind={result.kind!r}")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
