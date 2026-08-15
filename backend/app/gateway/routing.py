"""Eval-gated, explainable provider routing (CORTEX / Agent 10).

Routing must never invent smart behavior on a fresh database: without a
passing evidence gate, the configured provider (``EV_CHAT_PROVIDER``) is used
and the reason is recorded. Once the same three checks as the routing gate
(volume, health, latency) pass, cheap/high-frequency/privacy-sensitive work
prefers the local brain and hard reasoning prefers DeepSeek. Every selection
carries its reason and evidence so the audit trail explains every call.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, field

from app.config import settings

logger = logging.getLogger("ev.gateway.routing")

DEFAULT_MIN_CALLS = 5
DEFAULT_MAX_ERROR_RATE = 0.25
DEFAULT_MAX_P95_MS = 60_000.0

# Strategy modes that are cheap/high-frequency or privacy-sensitive by design.
CHEAP_MODES: frozenset[str] = frozenset(
    {
        "classification",
        "extraction_assist",
        "critic",
        "summarize",
        "quick",
        "memory",
    }
)
HARD_MODES: frozenset[str] = frozenset(
    {
        "technical",
        "complex",
        "planning",
        "reasoning",
        "analytical",
        "deep",
    }
)


@dataclass(frozen=True)
class ProviderSelection:
    """One explainable provider choice, recorded on every model call."""

    provider: str
    reason: str
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RoutingEvidenceCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class RoutingEvidenceGate:
    passed: bool
    checks: tuple[RoutingEvidenceCheck, ...] = ()

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "checks": [asdict(check) for check in self.checks],
        }


def evaluate_routing_evidence(
    stats: dict,
    *,
    min_calls: int = DEFAULT_MIN_CALLS,
    max_error_rate: float = DEFAULT_MAX_ERROR_RATE,
    max_p95_ms: float = DEFAULT_MAX_P95_MS,
) -> RoutingEvidenceGate:
    """Fail-closed evidence check, mirroring scripts/routing_gate.py."""

    totals = stats.get("totals") or {}
    calls = int(totals.get("calls") or 0)
    bad = int(totals.get("errors") or 0) + int(totals.get("blocked") or 0)
    bad_rate = bad / calls if calls else 0.0
    p95 = float(totals.get("p95_latency_ms") or 0.0)
    checks = (
        RoutingEvidenceCheck(
            "evidence_volume",
            calls >= min_calls,
            f"{calls} model calls (need >= {min_calls})",
        ),
        RoutingEvidenceCheck(
            "provider_health",
            calls > 0 and bad_rate <= max_error_rate,
            f"error+blocked rate {bad_rate:.2%} (max {max_error_rate:.0%})",
        ),
        RoutingEvidenceCheck(
            "latency_budget",
            calls > 0 and p95 <= max_p95_ms,
            f"p95 latency {p95:.0f}ms (max {max_p95_ms:.0f}ms)",
        ),
    )
    return RoutingEvidenceGate(
        passed=all(check.passed for check in checks),
        checks=checks,
    )


def _gate_evidence_dict(gate: RoutingEvidenceGate, stats: dict) -> dict:
    totals = stats.get("totals") or {}
    return {
        "gate": gate.to_dict(),
        "window": {
            "calls": totals.get("calls"),
            "errors": totals.get("errors"),
            "blocked": totals.get("blocked"),
            "p95_latency_ms": totals.get("p95_latency_ms"),
        },
    }


def routing_candidates() -> list[str]:
    """Providers that are actually configured for routing right now.

    ``echo``/``mock`` are offline dev doubles, not routing targets. DeepSeek
    is a candidate when it is the primary provider or an API key is set; the
    local provider is a candidate when a base URL/name override is configured.
    """

    candidates: set[str] = set()
    primary = settings.chat_provider
    if primary in ("deepseek", "local", "echo", "mock", "xai"):
        candidates.add(primary)
    if settings.deepseek_api_key or os.getenv("EV_DEEPSEEK_API_KEY"):
        candidates.add("deepseek")
    if settings.xai_api_key or os.getenv("EV_XAI_API_KEY"):
        candidates.add("xai")
    if (
        settings.local_model_base_url
        or os.getenv("EV_LOCAL_MODEL_BASE_URL")
        or settings.local_model_name != "llama3"
        or os.getenv("EV_LOCAL_MODEL_NAME")
    ):
        candidates.add("local")
    return sorted(candidates)


def select_provider(
    *,
    configured: str | None = None,
    evidence: dict | None = None,
    strategy: dict | None = None,
    privacy_sensitive: bool = False,
    min_calls: int = DEFAULT_MIN_CALLS,
    max_error_rate: float = DEFAULT_MAX_ERROR_RATE,
    max_p95_ms: float = DEFAULT_MAX_P95_MS,
) -> ProviderSelection:
    """Choose a provider and always return the reason it won.

    ``evidence`` is the ``model_call_stats`` shape. ``None`` (or a failed
    gate) means the configured provider is used fail-closed; smart routing is
    never invented from an empty database.
    """

    configured = configured or settings.chat_provider
    candidates = routing_candidates()
    if len(candidates) < 2:
        return ProviderSelection(
            provider=configured,
            reason="single_provider_routing_noop",
            evidence={
                "candidates": candidates,
                "note": "routing between providers is a no-op with one provider",
            },
        )
    if evidence is None:
        return ProviderSelection(
            provider=configured,
            reason="configured_fail_closed_no_evidence",
            evidence={"gate": None, "note": "no model-call evidence available"},
        )
    gate = evaluate_routing_evidence(
        evidence,
        min_calls=min_calls,
        max_error_rate=max_error_rate,
        max_p95_ms=max_p95_ms,
    )
    gate_evidence = _gate_evidence_dict(gate, evidence)
    if not gate.passed:
        return ProviderSelection(
            provider=configured,
            reason="routing_gate_failed_fail_closed",
            evidence=gate_evidence,
        )

    mode = str((strategy or {}).get("mode") or "").lower()
    if privacy_sensitive or mode in CHEAP_MODES:
        return ProviderSelection(
            provider="local",
            reason="cheap_privacy_sensitive_routed_local",
            evidence={**gate_evidence, "strategy_mode": mode, "privacy_sensitive": privacy_sensitive},
        )
    if mode in HARD_MODES:
        return ProviderSelection(
            provider="deepseek",
            reason="hard_reasoning_routed_deepseek",
            evidence={**gate_evidence, "strategy_mode": mode},
        )
    return ProviderSelection(
        provider=configured,
        reason="gate_passed_default_configured",
        evidence={**gate_evidence, "strategy_mode": mode},
    )
