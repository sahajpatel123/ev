"""Collector agent: sample derived perception and post to live channels.

The agent posts through the authenticated live-ingestion endpoints:

* ``POST /v1/live/events`` (batch) when no explicit channel id is configured;
* ``POST /v1/live/channels/{id}/events`` when ``EV_<KIND>_CHANNEL_ID`` is set
  (channel ids come from ``GET /v1/live/channels`` or ``POST /v1/live/channels``).

Every channel has a fail-closed privacy default: screen and audio events are
``sensitive`` (derived text/hints only, never raw pixels or audio), and
location events are ``private`` (coarse place/presence only, never exact
coordinates).  Per-channel overrides are available via ``EV_SCREEN_PRIVACY``,
``EV_AUDIO_PRIVACY`` and ``EV_LOCATION_PRIVACY`` (or the legacy global
``EV_LIVE_PRIVACY``), but the defaults are deliberately restrictive.

Delivery is offline-first: when the API is unreachable (or returns 5xx) the
events are written to a bounded local queue with idempotency keys, replayed
on the next loop, and deduplicated server-side by content hash.  Retries use
exponential backoff capped at 10 minutes; 4xx failures are quarantined
(dropped) because retrying them cannot succeed.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

import httpx

from clients.collectors.audio import audio_scene
from clients.collectors.location import location_context
from clients.collectors.queue import CollectorQueue
from clients.collectors.screen import Runner, ScreenState, screen_state

DEFAULT_API_URL = "http://127.0.0.1:8000"
MAX_BACKOFF_SECONDS = 600.0


def _api_url() -> str:
    return os.environ.get("EV_API_URL", DEFAULT_API_URL).rstrip("/")


def _api_key() -> str:
    key = os.environ.get("EV_API_KEY", "")
    if not key:
        raise SystemExit("EV_API_KEY is not set (export EV_API_KEY=... before running)")
    return key


def _device_id() -> str | None:
    return os.environ.get("EV_DEVICE_ID")


CHANNEL_DEFAULTS = {
    "screen-activity": "sensitive",
    "audio-ambient": "sensitive",
    "location-coarse": "private",
}

CHANNEL_ENV = {
    "screen-activity": ("EV_SCREEN_PRIVACY", "EV_SCREEN_CHANNEL_ID"),
    "audio-ambient": ("EV_AUDIO_PRIVACY", "EV_AUDIO_CHANNEL_ID"),
    "location-coarse": ("EV_LOCATION_PRIVACY", "EV_LOCATION_CHANNEL_ID"),
}


def _channel_privacy(channel: str) -> str:
    """Privacy level for one collector channel (defaults fail closed)."""
    override = CHANNEL_ENV.get(channel, (None, None))[0]
    if override and os.environ.get(override):
        return os.environ[override]
    global_override = os.environ.get("EV_LIVE_PRIVACY")
    if global_override:
        return global_override
    return CHANNEL_DEFAULTS.get(channel, "sensitive")


def _channel_id(channel: str) -> str | None:
    """Explicit channel id from the environment, when one is configured."""
    env_name = CHANNEL_ENV.get(channel, (None, None))[1]
    if env_name is None:
        return None
    return os.environ.get(env_name) or None


class FocusTracker:
    """Tracks how long the current (app, document) focus has been continuous."""

    def __init__(self, now: Callable[[], float] | None = None) -> None:
        self._now = now or time.monotonic
        self._key: tuple[str, str] | None = None
        self._started_at: float | None = None

    def focus_seconds(self, state: ScreenState) -> float:
        key = (state.app or "", state.document or "")
        now = self._now()
        if self._key != key or self._started_at is None:
            self._key = key
            self._started_at = now
        return max(0.0, now - self._started_at)


_focus_tracker = FocusTracker()


def _collect_screen(
    screen_runner: Runner | None = None,
    tracker: FocusTracker | None = None,
) -> dict | None:
    if tracker is None:
        tracker = _focus_tracker
    state = screen_state(screen_runner)
    payload = state.to_payload()
    if not payload:
        return None
    payload["focus_seconds"] = int(tracker.focus_seconds(state))
    return payload


def _collect_events(
    *,
    screen_runner: Runner | None = None,
    tracker: FocusTracker | None = None,
) -> dict[str, list[dict]]:
    """Return {channel_name: [LiveEventCreate-like dicts]} for this sample."""
    events: dict[str, list[dict]] = {}
    device_id = _device_id()
    occurred_at = datetime.now(UTC).isoformat()

    screen = _collect_screen(screen_runner, tracker)
    if screen:
        events.setdefault("screen-activity", []).append(
            {
                "event_type": "focus_change",
                "payload": screen,
                "device_id": device_id,
                "occurred_at": occurred_at,
            }
        )

    scene = audio_scene()
    if scene:
        events.setdefault("audio-ambient", []).append(
            {
                "event_type": "scene",
                "payload": scene,
                "device_id": device_id,
                "occurred_at": occurred_at,
            }
        )

    location = location_context()
    if location:
        events.setdefault("location-coarse", []).append(
            {
                "event_type": "location_change",
                "payload": location,
                "device_id": device_id,
                "occurred_at": occurred_at,
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
    channel_id = _channel_id(channel)
    if channel_id is not None:
        response = await client.post(
            f"/v1/live/channels/{channel_id}/events",
            json=events,
        )
    else:
        payload = {
            "channel": channel,
            "kind": kind,
            "privacy_level": _channel_privacy(channel),
            "events": events,
        }
        response = await client.post("/v1/live/events", json=payload)
    response.raise_for_status()


async def _post_record(client: httpx.AsyncClient, record: dict) -> httpx.Response:
    """Re-post one queued record exactly as it was enqueued."""
    headers = None
    key = record.get("idempotency_key")
    if key:
        headers = {"Idempotency-Key": str(key)}
    channel_id = record.get("channel_id")
    if channel_id:
        return await client.post(
            f"/v1/live/channels/{channel_id}/events",
            json=record["events"],
            headers=headers,
        )
    payload = {
        "channel": record["channel"],
        "kind": record["kind"],
        "privacy_level": record["privacy_level"],
        "events": record["events"],
    }
    return await client.post("/v1/live/events", json=payload, headers=headers)


async def collect_once(
    client: httpx.AsyncClient,
    *,
    screen_runner: Runner | None = None,
    tracker: FocusTracker | None = None,
) -> dict[str, int]:
    """Sample all collectors once and post each channel's events directly."""
    channels = _collect_events(screen_runner=screen_runner, tracker=tracker)
    counts: dict[str, int] = {}
    for channel, events in channels.items():
        kind = channel.split("-", 1)[0]
        await _post_channel(client, channel=channel, kind=kind, events=events)
        counts[channel] = len(events)
    return counts


async def collect_with_offline(
    client: httpx.AsyncClient,
    queue: CollectorQueue,
    *,
    screen_runner: Runner | None = None,
    tracker: FocusTracker | None = None,
) -> dict:
    """Sample once; post live or queue the batch when the API is unavailable."""
    channels = _collect_events(screen_runner=screen_runner, tracker=tracker)
    posted: dict[str, int] = {}
    queued: dict[str, int] = {}
    rejected: dict[str, int] = {}
    for channel, events in channels.items():
        kind = channel.split("-", 1)[0]
        try:
            await _post_channel(client, channel=channel, kind=kind, events=events)
            posted[channel] = len(events)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code >= 500:
                queue.enqueue(
                    channel=channel,
                    kind=kind,
                    events=events,
                    channel_id=_channel_id(channel),
                    privacy_level=_channel_privacy(channel),
                )
                queued[channel] = len(events)
            else:
                # 4xx cannot succeed on retry; reject instead of queueing.
                rejected[channel] = len(events)
        except httpx.TransportError:
            queue.enqueue(
                channel=channel,
                kind=kind,
                events=events,
                channel_id=_channel_id(channel),
                privacy_level=_channel_privacy(channel),
            )
            queued[channel] = len(events)
    return {
        "posted": posted,
        "queued": queued,
        "rejected": rejected,
        "failed": bool(queued),
    }


async def sync_queue(
    client: httpx.AsyncClient,
    queue: CollectorQueue,
) -> dict:
    """Replay the offline queue; 2xx/409-style dedupe syncs, 4xx quarantines,
    5xx/network failures keep the queue intact for the next backoff round."""
    records = queue.records()
    try:
        raw_count = len(queue.path.read_text(encoding="utf-8").splitlines())
    except OSError:
        raw_count = 0
    malformed = max(0, raw_count - len(records))
    removed: set[str] = set()
    synced = 0
    quarantined = 0
    failed = False
    for record in records:
        key = str(record.get("idempotency_key") or "")
        try:
            response = await _post_record(client, record)
            response.raise_for_status()
            if key:
                removed.add(key)
            synced += 1
        except (KeyError, TypeError, ValueError):
            # Malformed record: quarantine it so one bad line cannot wedge the queue.
            if key:
                removed.add(key)
            quarantined += 1
        except httpx.HTTPStatusError as exc:
            if 400 <= exc.response.status_code < 500:
                if key:
                    removed.add(key)
                quarantined += 1
            else:
                failed = True
                break
        except httpx.TransportError:
            failed = True
            break
    if removed:
        queue.remove(removed)
    elif malformed:
        queue.prune()
    return {
        "synced": synced,
        "quarantined": quarantined + malformed,
        "remaining": len(queue.records()),
        "failed": failed,
    }


def _backoff_seconds(interval_seconds: int, consecutive_failures: int) -> float:
    if consecutive_failures <= 0:
        return float(interval_seconds)
    return min(
        float(interval_seconds) * (2 ** min(consecutive_failures, 6)),
        MAX_BACKOFF_SECONDS,
    )


async def run_agent(*, interval_seconds: int, once: bool = False) -> None:
    headers = {"Authorization": f"Bearer {_api_key()}"}
    queue = CollectorQueue()
    consecutive_failures = 0
    async with httpx.AsyncClient(base_url=_api_url(), headers=headers, timeout=15) as client:
        while True:
            if len(queue) > 0:
                try:
                    summary = await sync_queue(client, queue)
                    if summary["synced"] or summary["quarantined"]:
                        print(f"[collector] queue sync: {summary}")
                    consecutive_failures = consecutive_failures + 1 if summary["failed"] else 0
                except Exception as exc:  # noqa: BLE001 - keep the 24/7 loop alive
                    print(f"[collector] queue sync failed, queue kept: {exc}")
                    consecutive_failures += 1
            try:
                counts = await collect_with_offline(client, queue)
                if counts["posted"] or counts["queued"] or counts["rejected"]:
                    parts = [f"{key}={value}" for key, value in counts["posted"].items()]
                    parts += [f"{key}=queued{value}" for key, value in counts["queued"].items()]
                    parts += [f"{key}=rejected{value}" for key, value in counts["rejected"].items()]
                    print("[collector] " + ", ".join(parts))
                consecutive_failures = consecutive_failures + 1 if counts["failed"] else 0
            except Exception as exc:  # noqa: BLE001 - keep the 24/7 loop alive
                print(f"[collector] live ingestion failed: {exc}")
                consecutive_failures += 1
            if once:
                return
            await asyncio.sleep(_backoff_seconds(interval_seconds, consecutive_failures))


def main() -> None:
    parser = argparse.ArgumentParser(description="EV privacy-preserving perception collector")
    parser.add_argument("--once", action="store_true", help="collect a single sample and exit")
    parser.add_argument("--interval", type=int, default=30, help="loop interval seconds")
    args = parser.parse_args()
    pid_file = os.environ.get("EV_COLLECTOR_PID_FILE")
    if pid_file:
        with suppress(OSError):
            Path(pid_file).write_text(str(os.getpid()), encoding="utf-8")
    asyncio.run(run_agent(interval_seconds=args.interval, once=args.once))


if __name__ == "__main__":
    main()
