"""Turn cancellation registry: named tokens for ops-turns + stop-all.

Ops-turns register a token under their turn id; the cancel route (or the
stop button) flips it and the turn aborts between steps. Kernel-loop
mid-turn cancellation lands in WP14; panic (deny new effects) is separate.
"""
from __future__ import annotations

from .turn import CancelToken

_tokens: dict[str, CancelToken] = {}


def reset() -> None:
    """Clear the registry. Tests only."""
    _tokens.clear()


def new_token(turn_id: str) -> CancelToken:
    token = CancelToken()
    _tokens[turn_id] = token
    return token


def get_token(turn_id: str) -> CancelToken | None:
    return _tokens.get(turn_id)


def cancel_turn(turn_id: str) -> dict:
    token = _tokens.get(turn_id)
    if token is None:
        return {"ok": False, "verified": True, "evidence": {},
                "error": "unknown or finished turn", "undo": None}
    token.cancel()
    return {"ok": True, "verified": True, "evidence": {"turn_id": turn_id},
            "error": None, "undo": None}


def cancel_all() -> dict:
    n = 0
    for token in list(_tokens.values()):
        token.cancel()
        n += 1
    _tokens.clear()
    return {"ok": True, "verified": True, "evidence": {"cancelled": n},
            "error": None, "undo": None}


def active_turns() -> list[str]:
    return [tid for tid, tok in _tokens.items() if not tok.cancelled]
