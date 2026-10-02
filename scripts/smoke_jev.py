"""Live OpenRouter JEV smoke test (synthetic-only, opt-in).

Two layers:

1. **Catalogue probe (no key needed):** verifies the model EV is configured to
   use actually exists on OpenRouter and has the expected shape. Measured
   2026-10-01: ``typesafe/jev-1.13`` is ``text->decisions``, 32K context,
   $0.042/1M prompt, no sampling parameters, and is served through OpenRouter's
   native typed Decisions API at
   ``POST https://openrouter.ai/api/alpha/decisions``.
2. **Provider smoke (key + egress + consent):** proves the real decision
   contract with synthetic prompts only — typed choice, gateway audit, raw
   media refusal, model-override refusal, usage/cost receipt.

Run from ``backend/`` with the key in the environment (never in git)::

    EV_JEV_ENABLED=true EV_ALLOW_REMOTE_CHAT=true \
    EV_OPENROUTER_API_KEY=... uv run python ../scripts/smoke_jev.py

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

CATALOGUE_URL = "https://openrouter.ai/api/v1/models/typesafe/jev-1.13/endpoints"


async def catalogue_probe(report: dict) -> bool:
    """Verify the configured JEV model exists and has the expected shape."""

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
        ok = (
            facts["id"] == "typesafe/jev-1.13"
            and facts["modality"] == "text->decisions"
            and facts["context_length"] == 32000
            and facts["supported_parameters"] == []
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
    if not getattr(settings, "jev_enabled", False):
        print("SKIP: EV_JEV_ENABLED is not true.")
        _write_report(report)
        return 2
    from app.compliance.policy import remote_processing_allowed

    if not remote_processing_allowed("chat_egress"):
        print("SKIP: EV_ALLOW_REMOTE_CHAT is not enabled.")
        _write_report(report)
        return 2

    from app.contracts import RequestEnvelope
    from app.gateway.openrouter_jev import (
        JevQuestion,
        OpenRouterEgressDenied,
        OpenRouterJevProvider,
        OpenRouterJevUnavailable,
    )
    from app.gateway.service import ModelGateway

    provider = OpenRouterJevProvider()
    report["model"] = provider.default_model
    report["base_url"] = provider.base_url
    try:
        await provider._require_active_chat_egress_consent()
    except OpenRouterEgressDenied as exc:
        print(f"SKIP: {exc}")
        _write_report(report)
        return 2

    # 1. Typed choice, direct provider call.
    route = {
        "route": JevQuestion(
            type="choice",
            instructions="Pick the safe route for a read-only question.",
            criteria={
                "read_only": "Only read data; no writes are needed.",
                "clarify": "The request is ambiguous.",
                "refuse": "The request must be refused.",
            },
        )
    }
    tick = time.perf_counter()
    try:
        result = await provider.decide(
            {"request": "Read the visible settings only."}, route
        )
        latency_ms = round((time.perf_counter() - tick) * 1000, 1)
        answer = result.answers["route"]
        check(
            "typed_choice",
            answer.choice in {"read_only", "clarify", "refuse"},
            f"choice={answer.choice} latency_ms={latency_ms} model={result.model}",
        )
        report["decision"] = {
            "choice": answer.choice,
            "latency_ms": latency_ms,
            "model": result.model,
            "usage": dict(result.usage),
            "response_id": result.response_id,
        }
    except OpenRouterJevUnavailable as exc:
        check("typed_choice", False, f"provider error: {exc}")
        report["decision_error"] = str(exc)

    # 2. The same choice through the gateway (privacy/cost/audit seam).
    gateway = ModelGateway(provider)
    call = await gateway.decide(
        {"request": "Read the visible settings only."},
        route,
        envelope=RequestEnvelope(request_id="jev-smoke-decision", strategy={"mode": "smoke"}),
    )
    check(
        "gateway_typed_decision",
        call.status == "ok" and bool(call.decision_answers),
        f"status={call.status} error={call.error or ''}",
    )
    report["gateway_decision"] = {
        "status": call.status,
        "latency_ms": call.latency_ms,
        "usage": dict(call.result.usage or {}),
        "audit": call.envelope.metadata.get("jev_decision"),
    }

    # 3. Raw media is refused locally (never sent).
    try:
        await provider.decide({"image": "data:image/png;base64,AA=="}, route)
        check("raw_media_refused", False, "raw pixels were NOT refused")
    except OpenRouterJevUnavailable as exc:
        check("raw_media_refused", "derived text only" in str(exc), type(exc).__name__)

    # 4. Model override is refused; EV_JEV_MODEL is the only model.
    try:
        await provider.decide("hello", route, model="some-other-model")
        check("model_override_refused", False, "model override was NOT refused")
    except OpenRouterJevUnavailable as exc:
        check("model_override_refused", "model overrides" in str(exc), type(exc).__name__)

    failures = [c for c in report["checks"] if not c["ok"]]
    _write_report(report)
    print(f"smoke: {len(report['checks']) - len(failures)}/{len(report['checks'])} passed")
    return 1 if failures else 0


def _write_report(report: dict) -> None:
    out = BACKEND / "backend" / "eval" / "jev-smoke.json"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, default=str))
        print(f"report: {out}")
    except OSError as exc:
        print(f"report write failed: {exc}")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
