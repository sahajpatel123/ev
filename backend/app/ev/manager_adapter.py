"""ManagerAdapter stub (G1.3) — complex-work manager boundary.

MiMo is the manager brain (see app/ev/model_router.py). This stub keeps the
G1.3 route scaffolded, not active: TurnController routes DELEGATED_JOB here
without changing voice/control architecture. Full specialist-agent runtime
is G3.
"""

from __future__ import annotations

from typing import Any

from app.config import settings


class ManagerAdapter:
    """Abstract manager — MimoManagerAdapter inherits."""

    async def submit(self, *, owner_turn: str, intent: Any, context: dict | None = None) -> dict:
        raise NotImplementedError


class MimoManagerAdapter(ManagerAdapter):
    """Scaffolded — validates routing, returns placeholder, never claims agents exist."""

    def __init__(self):
        self.provider = "mimo"
        self.model = (settings.mimo_model or "xiaomi/mimo-v2.6-flash").strip()
        self.available = bool((settings.openrouter_api_key or "").strip())
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
