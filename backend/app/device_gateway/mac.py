"""Remote Mac canary via existing Mac Control / Life Helper. Never fake success."""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.ev.apps import close_app, open_app

_IDEMPOTENCY: dict[str, dict[str, Any]] = {}
_TTL_S = 120.0


def _gc() -> None:
    now = time.time()
    for key, row in list(_IDEMPOTENCY.items()):
        if now - float(row.get("at") or 0) > _TTL_S:
            _IDEMPOTENCY.pop(key, None)


async def run_mac_canary(
    session: AsyncSession,
    *,
    action: str,
    actor: str,
    idempotency_key: str | None,
) -> dict[str, Any]:
    _gc()
    if idempotency_key:
        cached = _IDEMPOTENCY.get(idempotency_key)
        if cached is not None:
            return dict(cached["result"])
    if action == "open_calculator":
        result = await open_app(session, {"name": "Calculator"}, actor=actor)
    elif action == "close_calculator":
        result = await close_app(session, {"name": "Calculator"}, actor=actor)
    else:
        result = {
            "ok": False,
            "error": "unsupported_remote_action",
            "spoken": "That remote Mac action is not enabled in this sandbox pipeline.",
        }
    public = {
        "ok": bool(result.get("ok")),
        "error": result.get("error"),
        "spoken": result.get("spoken") or (
            "Done." if result.get("ok") else "Mac Control is unavailable."
        ),
        "verified": bool(
            result.get("ok")
            and (result.get("opened") or result.get("closed") or result.get("quit") or result.get("activated"))
        ),
        "source": result.get("source"),
    }
    if not public["ok"] and public["spoken"] in {"Done.", "Opened Calculator."}:
        public["spoken"] = "Mac Control is unavailable or unverified."
        public["ok"] = False
    if idempotency_key:
        _IDEMPOTENCY[idempotency_key] = {"at": time.time(), "result": public}
    return public
