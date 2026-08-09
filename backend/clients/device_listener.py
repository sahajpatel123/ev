"""EV device listener agent — a headless "ear" for the 24/7 runtime.

Runs on any always-on machine (Mac, Raspberry Pi, home server) and:

- heartbeats the runtime so the fleet sees the device as online and its
  listener state (battery, latency, listening/sleep/off);
- participates in wake arbitration by sending a wake intent with signal,
  proximity, and priority scores;
- pulls the cross-device sync snapshot so every device converges on the same
  runtime state.

The agent is intentionally small and dependency-light: it only needs httpx,
which the backend already ships. It degrades gracefully on network failures and
keeps heartbeating instead of crashing.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from typing import Any

import httpx

DEFAULT_API_URL = "http://127.0.0.1:8000"


def _api_url() -> str:
    return os.environ.get("EV_API_URL", DEFAULT_API_URL).rstrip("/")


def _api_key() -> str:
    key = os.environ.get("EV_API_KEY", "")
    if not key:
        raise SystemExit("EV_API_KEY is not set (export EV_API_KEY=... before running)")
    return key


def _device_id() -> str:
    device = os.environ.get("EV_DEVICE_ID", "")
    if not device:
        raise SystemExit("EV_DEVICE_ID is not set (use a registered device UUID)")
    return device


class DeviceListener:
    """Heartbeat + wake-arbitration client for one registered EV device."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        device_id: str,
        *,
        battery_percent: float | None = None,
    ) -> None:
        self.client = client
        self.device_id = device_id
        self.battery_percent = battery_percent

    async def heartbeat(
        self,
        *,
        status: str = "ok",
        listener_state: str = "listening",
        latency_ms: int | None = None,
    ) -> dict:
        """Report device liveness to the runtime."""
        payload: dict[str, Any] = {
            "device_id": self.device_id,
            "status": status,
            "listener_state": listener_state,
        }
        if self.battery_percent is not None:
            payload["battery_percent"] = self.battery_percent
        if latency_ms is not None:
            payload["latency_ms"] = latency_ms
        response = await self.client.post("/v1/runtime/heartbeat", json=payload)
        response.raise_for_status()
        return response.json()

    async def wake(
        self,
        *,
        signal_score: float = 0.5,
        proximity_score: float = 0.5,
        priority: float = 0.5,
        payload: dict | None = None,
    ) -> dict:
        """Send one wake intent; the runtime arbitrates across the fleet."""
        response = await self.client.post(
            "/v1/runtime/wake",
            json=[
                {
                    "device_id": self.device_id,
                    "signal_score": signal_score,
                    "proximity_score": proximity_score,
                    "priority": priority,
                    "payload": payload or {},
                }
            ],
        )
        response.raise_for_status()
        return response.json()

    async def sync_state(self, since: str | None = None) -> dict:
        """Fetch the convergent cross-device runtime snapshot."""
        params: dict[str, str | int] = {"limit": 200}
        if since:
            params["since"] = since
        response = await self.client.get("/v1/runtime/sync", params=params)
        response.raise_for_status()
        return response.json()

    async def run_loop(
        self,
        *,
        interval_seconds: int = 30,
        wake_once: bool = False,
        signal_score: float = 0.5,
        proximity_score: float = 0.5,
        priority: float = 0.5,
    ) -> None:
        """Heartbeat forever (or once when ``wake_once`` is set) and wake once."""
        first = True
        while True:
            try:
                await self.heartbeat()
                print(f"[listener] heartbeat ok ({self.device_id})")
                if wake_once and first:
                    outcome = await self.wake(
                        signal_score=signal_score,
                        proximity_score=proximity_score,
                        priority=priority,
                    )
                    print(f"[listener] wake outcome: {outcome.get('state')}")
                    return
            except (httpx.HTTPError, httpx.HTTPStatusError) as exc:
                print(f"[listener] heartbeat failed, retrying: {exc}")
            first = False
            await asyncio.sleep(max(1, interval_seconds))


def main() -> None:
    parser = argparse.ArgumentParser(description="EV device listener agent")
    parser.add_argument("--once", action="store_true", help="Heartbeat and exit")
    parser.add_argument("--wake-once", action="store_true", help="Heartbeat then wake once")
    parser.add_argument("--interval", type=int, default=int(os.environ.get("EV_LISTENER_INTERVAL", "30")))
    parser.add_argument("--signal", type=float, default=0.5)
    parser.add_argument("--proximity", type=float, default=0.5)
    parser.add_argument("--priority", type=float, default=0.5)
    parser.add_argument("--battery", type=float, default=None)
    args = parser.parse_args()

    listener = DeviceListener(
        httpx.AsyncClient(
            base_url=_api_url(),
            headers={"Authorization": f"Bearer {_api_key()}"},
            timeout=15.0,
        ),
        _device_id(),
        battery_percent=args.battery,
    )

    async def _run() -> None:
        if args.once:
            await listener.heartbeat()
            print("[listener] one-shot heartbeat ok")
            return
        await listener.run_loop(
            interval_seconds=args.interval,
            wake_once=args.wake_once,
            signal_score=args.signal,
            proximity_score=args.proximity,
            priority=args.priority,
        )

    asyncio.run(_run())


if __name__ == "__main__":
    main()
