"""Voice-edge HTTP client to the Cognitive Kernel, and Mac-execute callback."""

from __future__ import annotations

from typing import Any

import httpx

from app.cognitive.kernel import KernelResult
from app.cognitive.mode import kernel_url, mac_execute_url
from app.config import settings


def _headers() -> dict[str, str]:
    key = (getattr(settings, "master_key", None) or "").strip()
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


async def post_turn(base: str | None = None, **kwargs: Any) -> KernelResult:
    url = (base or kernel_url()).rstrip("/") + "/v1/cognitive/turn"
    payload = {
        "transcript": kwargs.get("transcript") or "",
        "live_session_id": kwargs.get("live_session_id"),
        "device_id": kwargs.get("device_id"),
        "modality": kwargs.get("modality") or "voice",
    }
    timeout = httpx.Timeout(connect=5.0, read=120.0, write=15.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json=payload, headers=_headers())
        response.raise_for_status()
        data = response.json()
    return KernelResult(
        spoken=str(data.get("spoken") or ""),
        kind=str(data.get("kind") or "muse"),
        unavailable=bool(data.get("unavailable")),
        persist=bool(data.get("persist")),
        steering_version=int(data.get("steering_version") or 0),
        goal_id=data.get("goal_id"),
        latency_ms=float(data.get("latency_ms") or 0.0),
        tool_calls=int(data.get("tool_calls") or 0),
    )


async def execute_on_mac(
    name: str,
    arguments: dict[str, Any],
    *,
    live_session_id: str | None,
) -> dict[str, Any]:
    """Kernel asks the Voice Edge to use MacControl / live tool dispatch."""

    url = mac_execute_url() + "/v1/cognitive/mac-execute"
    timeout = httpx.Timeout(connect=3.0, read=90.0, write=10.0, pool=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                url,
                json={
                    "name": name,
                    "arguments": arguments or {},
                    "live_session_id": live_session_id,
                },
                headers=_headers(),
            )
            if response.status_code >= 400:
                return {
                    "ok": False,
                    "error": "DEVICE_OFFLINE",
                    "diagnosis": "DEVICE_OFFLINE",
                    "spoken": "I can't reach the Mac hands right now.",
                }
            data = response.json()
            return data if isinstance(data, dict) else {"ok": False, "result": data}
    except Exception:
        return {
            "ok": False,
            "error": "DEVICE_OFFLINE",
            "diagnosis": "DEVICE_OFFLINE",
            "spoken": "The Mac executor isn't reachable.",
        }
