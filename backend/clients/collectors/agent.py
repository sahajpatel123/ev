"""Collector agent: sample derived perception and post to live channels."""

from __future__ import annotations

import argparse
import asyncio
import os

import httpx

from clients.collectors.audio import audio_scene
from clients.collectors.location import location_context
from clients.collectors.screen import Runner, screen_state

DEFAULT_API_URL = "http://127.0.0.1:8000"


def _api_url() -> str:
    return os.environ.get("EV_API_URL", DEFAULT_API_URL).rstrip("/")


def _api_key() -> str:
    key = os.environ.get("EV_API_KEY", "")
    if not key:
        raise SystemExit("EV_API_KEY is not set (export EV_API_KEY=... before running)")
    return key


def _device_id() -> str | None:
    return os.environ.get("EV_DEVICE_ID")


def _privacy_level() -> str:
    return os.environ.get("EV_LIVE_PRIVACY", "normal")


def _collect_screen(screen_runner: Runner | None) -> dict | None:
    state = screen_state(screen_runner)
    payload = state.to_payload()
    return payload or None


def _collect_events(*, screen_runner: Runner | None = None) -> dict[str, list[dict]]:
    """Return {channel_name: [LiveEventCreate-like dicts]} for this sample."""
    events: dict[str, list[dict]] = {}
    device_id = _device_id()

    screen = _collect_screen(screen_runner)
    if screen:
        events.setdefault("screen-activity", []).append(
            {
                "event_type": "focus_change",
                "payload": screen,
                "device_id": device_id,
            }
        )

    scene = audio_scene()
    if scene:
        events.setdefault("audio-ambient", []).append(
            {
                "event_type": "scene",
                "payload": scene,
                "device_id": device_id,
            }
        )

    location = location_context()
    if location:
        events.setdefault("location-coarse", []).append(
            {
                "event_type": "location_change",
                "payload": location,
                "device_id": device_id,
            }
        )
    return events


async def _post_channel(
    client: httpx.AsyncClient,
    *,
    channel: str,
    kind: str,
    events: list[dict],
) -> None:
    payload = {
        "channel": channel,
        "kind": kind,
        "privacy_level": _privacy_level(),
        "events": events,
    }
    response = await client.post("/v1/live/events", json=payload)
    response.raise_for_status()


async def collect_once(
    client: httpx.AsyncClient,
    *,
    screen_runner: Runner | None = None,
) -> dict[str, int]:
    """Sample all collectors once and post each channel's events."""
    channels = _collect_events(screen_runner=screen_runner)
    counts: dict[str, int] = {}
    for channel, events in channels.items():
        kind = channel.split("-", 1)[0]
        await _post_channel(client, channel=channel, kind=kind, events=events)
        counts[channel] = len(events)
    return counts


async def run_agent(*, interval_seconds: int, once: bool = False) -> None:
    headers = {"Authorization": f"Bearer {_api_key()}"}
    async with httpx.AsyncClient(base_url=_api_url(), headers=headers, timeout=15) as client:
        while True:
            try:
                counts = await collect_once(client)
                if counts:
                    print("[collector] posted " + ", ".join(f"{k}={v}" for k, v in counts.items()))
                else:
                    print("[collector] nothing to report")
            except httpx.HTTPStatusError as exc:
                print(f"[collector] live ingestion failed: {exc.response.status_code} {exc.response.text[:200]}")
            if once:
                return
            await asyncio.sleep(interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="EV privacy-preserving perception collector")
    parser.add_argument("--once", action="store_true", help="collect a single sample and exit")
    parser.add_argument("--interval", type=int, default=30, help="loop interval seconds")
    args = parser.parse_args()
    asyncio.run(run_agent(interval_seconds=args.interval, once=args.once))


if __name__ == "__main__":
    main()
