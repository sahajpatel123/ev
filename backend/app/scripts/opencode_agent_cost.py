"""Measure the opencode preamble tax per agent (``make opencode-agent-cost``).

Every opencode agent injects its own system preamble into each request, which
EV pays for on top of its own ~20k budgeted context. This script sends one
identical short prompt through each agent and prints opencode's own reported
token and cost numbers, so the choice of ``EV_OPENCODE_AGENT`` is evidence
based rather than assumed.

Each measurement runs in a fresh ephemeral session that is deleted afterwards.
Real model calls cost real money (a few cents per hundred runs at most).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from app.config import settings
from app.contracts import ChatMessage
from app.gateway.opencode import OpenCodeProvider, OpenCodeUnavailableError

PROMPT = "say EV_OPENCODE_OK"
SYSTEM = "You are EV. Answer with exactly the token the user asks for."
DEFAULT_AGENTS = ("ev-minimal", "plan", "general", "build")


async def measure(agent: str) -> dict:
    provider = OpenCodeProvider(agent=agent, session_reuse=False)
    started = time.perf_counter()
    result = await provider.chat(
        [ChatMessage(role="system", content=SYSTEM), ChatMessage(role="user", content=PROMPT)]
    )
    latency_ms = (time.perf_counter() - started) * 1000
    usage = result.usage
    return {
        "agent": agent,
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "cached": usage.get("cached_prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "cost_usd": usage.get("cost_usd", 0.0),
        "latency_ms": round(latency_ms, 1),
        "text": result.text.strip()[:40],
    }


async def main_async(agents: list[str]) -> int:
    print(f"opencode preamble cost — {settings.opencode_provider_id}/{settings.opencode_model}")
    print(f"prompt: {PROMPT!r} (system: {len(SYSTEM)} chars)")
    print("=" * 88)
    print(f"{'agent':<14}{'prompt_tok':>11}{'cached':>8}{'out_tok':>9}{'cost_usd':>13}{'latency_ms':>12}  reply")
    rows: list[dict] = []
    for agent in agents:
        try:
            row = await measure(agent)
        except OpenCodeUnavailableError as exc:
            print(f"{agent:<14} unavailable: {exc}")
            return 1
        except Exception as exc:  # noqa: BLE001 - one bad agent must not hide the rest
            print(f"{agent:<14} failed: {type(exc).__name__}: {exc}")
            continue
        rows.append(row)
        print(
            f"{row['agent']:<14}{row['prompt_tokens']:>11}{row['cached']:>8}"
            f"{row['completion_tokens']:>9}{row['cost_usd']:>13.8f}"
            f"{row['latency_ms']:>12}  {row['text']!r}"
        )
    print("=" * 88)
    if len(rows) > 1:
        cheapest = min(rows, key=lambda r: r["prompt_tokens"])
        dearest = max(rows, key=lambda r: r["prompt_tokens"])
        saved = dearest["prompt_tokens"] - cheapest["prompt_tokens"]
        print(
            f"{cheapest['agent']} is {saved} prompt tokens cheaper than "
            f"{dearest['agent']} per request "
            f"(${dearest['cost_usd'] - cheapest['cost_usd']:.8f} per call)"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("agents", nargs="*", default=list(DEFAULT_AGENTS))
    args = parser.parse_args()
    return asyncio.run(main_async(args.agents or list(DEFAULT_AGENTS)))


if __name__ == "__main__":
    sys.exit(main())
