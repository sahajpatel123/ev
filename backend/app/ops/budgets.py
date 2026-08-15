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
MODEL_PRICES_USD_PER_1M = {
    "deepseek": {"input": 0.27, "output": 1.10},
    "xai": {"input": 2.00, "output": 6.00},
    "echo": {"input": 0.0, "output": 0.0},
    "mock": {"input": 0.0, "output": 0.0},
    "default": {"input": 1.00, "output": 3.00},
}
