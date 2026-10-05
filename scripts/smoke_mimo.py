"""Live OpenRouter MiMo smoke test (synthetic-only, opt-in).

Two layers:

1. **Catalogue probe (no key needed):** verifies the model EV is configured to
   use actually exists on OpenRouter and has the expected shape. Measured
   2026-10-04: ``xiaomi/mimo-v2.6-flash`` is
   ``text+image+audio+video->text`` (multimodal in, text out), ctx 1048576,
   served through OpenRouter's OpenAI-compatible chat-completions API at
   ``POST https://openrouter.ai/api/v1/chat/completions``.
2. **Provider smoke (key + egress + consent):** proves the real contract with
   synthetic prompts only — structured choice, gateway audit, usage/cost
   receipt.

Run from ``backend/`` with the key in the environment (never in git)::

    EV_MIMO_ENABLED=true EV_ALLOW_REMOTE_CHAT=true \\
    EV_OPENROUTER_API_KEY=... uv run python ../scripts/smoke_mimo.py

Without a key/egress/consent the script prints SKIP and exits 2. It never
fabricates a pass and never writes secrets to the report.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "backend"))

CATALOGUE_URL = "https://openrouter.ai/api/v1/models/xiaomi/mimo-v2.6-flash/endpoints"


async def catalogue_probe(report: dict) -> bool:
    """Verify the configured MiMo model exists and has the expected shape."""

    import httpx

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(CATALOGUE_URL)
            response.raise_for_status()
        data = response.json().get("data") or {}
        architecture = data.get("architecture") or {}
        endpoints = data.get("endpoints") or []
        endpoint = endpoints[0] if endpoints else {}
        facts = {
            "id": data.get("id"),
            "modality": architecture.get("modality"),
            "context_length": endpoint.get("context_length"),
            "supported_parameters": endpoint.get("supported_parameters"),
            "pricing_prompt_per_token": (endpoint.get("pricing") or {}).get("prompt"),
        }
        report["catalogue"] = facts
        modality = str(facts["modality"] or "")
        ok = (
            facts["id"] == "xiaomi/mimo-v2.6-flash"
            and "text" in modality.split("->")[0].split("+")
            and modality.endswith("->text")
            and (facts["context_length"] or 0) >= 32000
        )
        print(
            f"[{'PASS' if ok else 'FAIL'}] catalogue: model={facts['id']} "
            f"modality={facts['modality']} ctx={facts['context_length']} "
            f"params={facts['supported_parameters']}"
        )
        return ok
    except Exception as exc:  # noqa: BLE001 - report the failure class
        report["catalogue"] = {"error": f"{type(exc).__name__}: {exc}"}
        print(f"[FAIL] catalogue probe: {type(exc).__name__}: {exc}")
        return False


async def main() -> int:
    from app.config import settings

    report: dict = {"checks": [], "catalogue": None}

    def check(name: str, ok: bool, detail: str = "") -> None:
        report["checks"].append({"name": name, "ok": bool(ok), "detail": str(detail)[:500]})
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail[:200]}")

    check("catalogue_model_shape", await catalogue_probe(report))

    key = (
        os.environ.get("EV_OPENROUTER_API_KEY")
        or getattr(settings, "openrouter_api_key", None)
        or ""
    ).strip()
    if not key:
        print("SKIP: EV_OPENROUTER_API_KEY is not set (live provider smoke needs a key).")
        _write_report(report)
        return 2
    if not getattr(settings, "mimo_enabled", False):
        print("SKIP: EV_MIMO_ENABLED is not true.")
        _write_report(report)
        return 2
    from app.compliance.policy import remote_processing_allowed

    if not remote_processing_allowed("chat_egress"):
        print("SKIP: EV_ALLOW_REMOTE_CHAT is not enabled.")
        _write_report(report)
        return 2

    from app.contracts import ChatMessage, RequestEnvelope
    from app.gateway.openrouter_mimo import (
        MimoEgressDenied,
        MimoProvider,
        MimoUnavailable,
    )
    from app.gateway.roles import DecisionQuestion, decide_via_role
    from app.gateway.service import ModelGateway

    provider = MimoProvider()
    report["model"] = provider.default_model
    report["base_url"] = provider.base_url
    try:
        await provider._authorize()
    except (MimoEgressDenied, MimoUnavailable) as exc:
        print(f"SKIP: {exc}")
        _write_report(report)
        return 2

    # 1. Structured choice, direct provider call.
    schema = {
        "type": "object",
        "properties": {"route": {"type": "string"}},
        "required": ["route"],
        "additionalProperties": False,
    }
    tick = time.perf_counter()
    try:
        result = await provider.chat_structured(
            [ChatMessage(role="user", content="Read the visible settings only.")],
            schema=schema,
            schema_name="smoke_route",
        )
        latency_ms = round((time.perf_counter() - tick) * 1000, 1)
        payload = json.loads(result.text or "{}")
        check(
            "structured_choice",
            isinstance(payload.get("route"), str) and bool(payload["route"].strip()),
            f"route={payload.get('route')} latency_ms={latency_ms} model={result.model}",
        )
        report["structured"] = {
            "route": payload.get("route"),
            "latency_ms": latency_ms,
            "model": result.model,
            "usage": dict(result.usage or {}),
        }
    except (MimoUnavailable, json.JSONDecodeError) as exc:
        check("structured_choice", False, f"provider error: {exc}")
        report["structured_error"] = str(exc)

    # 2. The same choice through the typed role seam (decision validation).
    route = {
        "route": DecisionQuestion(
            type="choice",
            instructions="Pick the safe route for a read-only question.",
            criteria={
                "read_only": "Only read data; no writes are needed.",
                "clarify": "The request is ambiguous.",
                "refuse": "The request must be refused.",
            },
        )
    }
    call = await decide_via_role(
        {"request": "Read the visible settings only."}, route, actor="smoke"
    )
    check(
        "typed_role_decision",
        call.status == "ok" and bool(call.decision_answers),
        f"status={call.status} error={call.error or ''}",
    )
    report["role_decision"] = {
        "status": call.status,
        "latency_ms": call.latency_ms,
        "usage": dict(call.result.usage or {}),
    }

    # 3. One chat turn through the gateway (privacy/cost/audit seam).
    gateway = ModelGateway(provider)
    chat_call = await gateway.chat(
        [ChatMessage(role="user", content="Reply with exactly: smoke ok.")],
        envelope=RequestEnvelope(request_id="mimo-smoke-chat", strategy={"mode": "smoke"}),
    )
    check(
        "gateway_chat",
        chat_call.status == "ok" and "smoke ok" in (chat_call.result.text or "").lower(),
        f"status={chat_call.status} error={chat_call.error or ''}",
    )
    report["gateway_chat"] = {
        "status": chat_call.status,
        "latency_ms": chat_call.latency_ms,
        "usage": dict(chat_call.result.usage or {}),
    }

    failures = [c for c in report["checks"] if not c["ok"]]
    _write_report(report)
    print(f"smoke: {len(report['checks']) - len(failures)}/{len(report['checks'])} passed")
    return 1 if failures else 0


def _write_report(report: dict) -> None:
    out = BACKEND / "backend" / "eval" / "mimo-smoke.json"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, default=str))
        print(f"report: {out}")
    except OSError as exc:
        print(f"report write failed: {exc}")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
