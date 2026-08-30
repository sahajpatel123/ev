"""In-process presence. TTL; never persist 'online forever'."""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID

_TTL_S = 45.0
_PRESENCE: dict[str, dict[str, Any]] = {}


def note(device_id: UUID | str, *, instance_id: str = "", state: str = "ready") -> None:
    key = str(device_id)
    _PRESENCE[key] = {
        "device_id": key,
        "instance_id": instance_id,
        "state": state,
        "seen_at": time.time(),
    }


def snapshot() -> dict[str, Any]:
    now = time.time()
    live = []
    stale = []
    for key, row in list(_PRESENCE.items()):
        age = now - float(row.get("seen_at") or 0)
        if age > _TTL_S * 4:
            _PRESENCE.pop(key, None)
            continue
        item = {**row, "age_s": round(age, 1)}
        if age <= _TTL_S:
            live.append(item)
        else:
            stale.append(item)
    return {"online": live, "stale": stale, "online_count": len(live)}
