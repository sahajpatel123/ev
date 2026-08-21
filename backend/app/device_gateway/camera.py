"""Ephemeral camera frames. Request-based. Never Memory OS."""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

_TTL_S = 90.0
_MAX_BYTES = 1_200_000
_STORE: dict[str, dict[str, Any]] = {}


def new_request(*, origin_device_id: str, target_device_id: str) -> str:
    request_id = uuid4().hex
    _STORE[request_id] = {
        "request_id": request_id,
        "origin_device_id": origin_device_id,
        "target_device_id": target_device_id,
        "jpeg_b64": None,
        "created_at": time.time(),
        "received_at": None,
    }
    return request_id


def put_frame(request_id: str, *, device_id: str, jpeg_b64: str) -> dict[str, Any]:
    _gc()
    row = _STORE.get(request_id)
    if row is None:
        raise KeyError("unknown camera request")
    if row["target_device_id"] != device_id:
        raise PermissionError("camera request is bound to another device")
    raw = jpeg_b64 or ""
    if len(raw) > _MAX_BYTES:
        raise ValueError("camera frame too large")
    row["jpeg_b64"] = raw
    row["received_at"] = time.time()
    return {k: v for k, v in row.items() if k != "jpeg_b64"} | {"bytes": len(raw)}


def get_frame(request_id: str) -> dict[str, Any] | None:
    _gc()
    return _STORE.get(request_id)


def _gc() -> None:
    now = time.time()
    for key, row in list(_STORE.items()):
        if now - float(row.get("created_at") or 0) > _TTL_S:
            _STORE.pop(key, None)
