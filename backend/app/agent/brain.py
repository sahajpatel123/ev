"""Which brain runs an agent goal — one resolver, explicit and network-free.

``EV_AGENT_PROVIDER`` (or an explicit argument) names the brain:
``auto`` | ``muse`` | ``echo``.

``muse`` is Muse Spark on OpenRouter, the only brain; ``auto`` picks it when
its key is present and otherwise falls through to the offline double.
The choice is always reported back on the run receipt
(provider, model, whether it is the offline double), so nothing about the
run's provenance is hidden. Resolution never touches the network: a stale key
fails at call time with the provider's own typed error, which the loop turns
into an honest ``brain_unavailable`` receipt instead of a fake answer.

No silent fallback: if ``muse`` is named explicitly and has no key, the
resolver returns it with ``available=False`` and the loop refuses to run.
Only ``auto`` falls through, and only to the offline double (``echo``), which
is reported as ``degraded=True``. A retired name (deepseek, mimo, xai, ...)
raises instead of being mapped onto the current brain.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import settings

KNOWN_BRAINS = ("auto", "muse", "echo")
AUTO_ORDER = ("muse",)


@dataclass(frozen=True)
class BrainChoice:
    provider: str
    model: str
    reason: str
    available: bool
    degraded: bool = False

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "reason": self.reason,
            "available": self.available,
            "degraded": self.degraded,
        }


def _key(name: str) -> str:
    return str(getattr(settings, name, "") or "").strip()


def _candidate(provider: str) -> BrainChoice:
    if provider == "muse":
        from app.gateway.openrouter_muse import default_brain_model

        key = _key("openrouter_api_key")
        return BrainChoice(
            "muse", default_brain_model(),
            "openrouter key present (muse main brain)"
            if key else "EV_OPENROUTER_API_KEY is empty",
            bool(key),
        )
    return BrainChoice("echo", "echo-local", "offline double (no brain key configured)", True, degraded=True)


def resolve_agent_brain(preferred: str | None = None) -> BrainChoice:
    """Resolve the agent's brain without touching the network."""
    name = str(preferred or settings.agent_provider or "auto").strip().lower()
    if name not in KNOWN_BRAINS:
        known = ", ".join(KNOWN_BRAINS)
        raise ValueError(f"unknown agent brain {name!r}; known: {known}")
    if name != "auto":
        return _candidate(name)
    for candidate in AUTO_ORDER:
        choice = _candidate(candidate)
        if choice.available:
            return choice
    return _candidate("echo")


def build_agent_provider(choice: BrainChoice):
    """Instantiate the chat provider for one resolution. Raises on no key."""
    if choice.provider == "muse":
        from app.gateway.openrouter_muse import MuseSparkProvider

        return MuseSparkProvider()
    from app.gateway.providers import EchoProvider

    return EchoProvider()


def brain_status() -> dict:
    """Doctor-facing snapshot: every candidate and the auto pick."""
    return {
        "provider_setting": str(settings.agent_provider or "auto"),
        "auto_pick": resolve_agent_brain().to_dict(),
        "candidates": {name: _candidate(name).to_dict() for name in AUTO_ORDER},
    }
