"""Operational budgets: single source of truth for latency and cost limits.

Values mirror docs/EVALUATION.md §8 and docs/DEPLOYMENT.md §10. The eval gates,
the ops metrics endpoint, and the ops center all read from here so budgets are
enforced consistently instead of drifting across surfaces.
"""

LATENCY_BUDGETS_MS = {
    "event_ack": 1000,
    "chat_first_token": 1500,
    "timeline_browse": 500,
    "tactical_briefing": 3000,
    "tactical_quick_card": 800,
}

HEALTH_BUDGET_MS = 200

MONTHLY_COST_BUDGET_USD = 40.0

# Estimated USD per 1M tokens, by provider. These are engineering estimates,
# not billing quotes; update when provider pricing changes.
# Cost normally comes from the provider-reported receipt
# (log_model_call cost_source=openrouter_reported). The estimate below is
# used ONLY for pre-call cap projection and missing-usage audit — never
# reported as a measured number.
MODEL_PRICES_USD_PER_1M = {
    # Muse Spark 1.3 Contributor on OpenRouter: $0.10 / $0.20 per 1M tokens,
    # from the /api/v1/models listing read on 2026-10-25.
    "muse": {"input": 0.10, "output": 0.20},
    "echo": {"input": 0.0, "output": 0.0},
    "mock": {"input": 0.0, "output": 0.0},
    "default": {"input": 1.00, "output": 3.00},
}
