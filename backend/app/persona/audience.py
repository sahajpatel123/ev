"""Audience modes (spec §3). Resolved by the caller; validated here."""

from __future__ import annotations

from app.persona.context import PersonaContext

PRIVATE = "private"
ON_BEHALF = "on_behalf"
COMPANY = "company"

AUDIENCES = (PRIVATE, ON_BEHALF, COMPANY)


def resolve_audience(ctx: PersonaContext) -> str:
    """Return the effective audience. Unknown values fall back to private.

    On-behalf is set by the caller whenever text is drafted as the owner;
    this module never infers it from message content.
    """
    audience = (ctx.audience or "").strip().lower()
    if audience in AUDIENCES:
        return audience
    return PRIVATE
