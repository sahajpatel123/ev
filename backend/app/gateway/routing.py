"""Single-brain provider selection (CORTEX / Agent 10).

Two-model workspace: the configured provider (``EV_CHAT_PROVIDER``) always
wins and the reason is recorded. There is no tournament and never a silent
substitute for another reasoning provider.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from app.config import settings


@dataclass(frozen=True)
class ProviderSelection:
    """One explainable provider choice, recorded on every model call."""

    provider: str
    reason: str
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def routing_candidates() -> list[str]:
    """The single configured provider. No tournament: one brain, no substitutes."""

    primary = (settings.chat_provider or "echo").strip().lower()
    return [primary]


def select_provider(
    *,
    configured: str | None = None,
    evidence: dict | None = None,
    strategy: dict | None = None,
    privacy_sensitive: bool = False,
    min_calls: int = 5,
    max_error_rate: float = 0.25,
    max_p95_ms: float = 60_000.0,
) -> ProviderSelection:
    """Choose a provider and always return the reason it won.

    Two-model workspace: the configured provider always wins. The extra
    knobs are accepted for call compatibility and reported back, but
    routing between providers is a no-op with one brain.
    """

    del evidence, strategy, privacy_sensitive, min_calls, max_error_rate, max_p95_ms
    configured = (configured or settings.chat_provider or "echo").strip().lower()
    return ProviderSelection(
        provider=configured,
        reason="single_brain_configured",
        evidence={
            "candidates": [configured],
            "configured": configured,
            "note": "one brain; routing is a no-op",
        },
    )
