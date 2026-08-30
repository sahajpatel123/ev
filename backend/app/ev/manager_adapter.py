"""ManagerAdapter stub (G1.3) — DeepSeek complex-work manager boundary.

G1.3 only needs the route to exist and be scaffolded, not active.
Full specialist-agent runtime is G3.  TurnController routes DELEGATED_JOB
here without changing voice/control architecture.
"""

from __future__ import annotations

from typing import Any

from app.config import settings


class ManagerAdapter:
    """Abstract manager — future DeepSeekManagerAdapter will inherit."""

    async def submit(self, *, owner_turn: str, intent: Any, context: dict | None = None) -> dict:
        raise NotImplementedError


class DeepSeekManagerAdapter(ManagerAdapter):
    """Scaffolded — validates routing, returns placeholder, never claims agents exist."""

    def __init__(self):
        self.provider = "deepseek"
        self.model = (settings.deepseek_model or "deepseek-v4-flash").strip()
        self.available = bool((settings.deepseek_api_key or "").strip())
        self.status = "scaffolded" if self.available else "not_active"

    async def submit(self, *, owner_turn: str, intent: Any, context: dict | None = None) -> dict:
        return {
            "ok": True,
            "stub": "manager_scaffolded",
            "provider": self.provider,
            "model": self.model,
            "status": self.status,
            "owner_turn": owner_turn,
            "note": "Manager is scaffolded in G1.3; specialist agents arrive in G3.",
        }

    def health(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "available": self.available,
            "status": self.status,
        }
