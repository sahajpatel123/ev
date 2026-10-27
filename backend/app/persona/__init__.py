"""Evie persona layer (persona.v2). Deterministic, offline, flag-gated.

THE interfaces for other lanes: build_persona_block(ctx) compiles the
prompt block; persona_guard(text, ctx) checks the finished reply.
"""

from __future__ import annotations

from app.persona.compile import build_persona_block
from app.persona.guard import persona_guard
from app.persona.settings import PERSONA_ENABLED, PERSONA_VERSION

__all__ = [
    "PERSONA_ENABLED",
    "PERSONA_VERSION",
    "build_persona_block",
    "persona_guard",
]
