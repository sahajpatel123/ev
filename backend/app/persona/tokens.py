"""Token estimation for persona budgets. Reuses the repo's estimator."""

from __future__ import annotations

from app.utils.text import token_estimate as _repo_estimate


def estimate(text: str) -> int:
    """Estimated tokens for one string (repo convention: ~4 chars/token)."""
    return _repo_estimate(text or "")


def estimate_parts(*parts: str) -> int:
    """Estimated tokens for several parts joined as the block joins them."""
    return estimate("\n\n".join(part for part in parts if part))
