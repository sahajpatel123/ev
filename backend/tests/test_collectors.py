"""Tests for the privacy-preserving OS-level perception collectors."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.ev.user_state import build_user_state
from app.models import LiveEvent
from app.routines.service import list_runs
from clients.cli import vision as cli_vision
from clients.collectors import _native
from clients.collectors import agent as collector_agent
from clients.collectors import location as location_module
from clients.collectors import screen as screen_module
from clients.collectors.agent import (
    FocusTracker,
    _backoff_seconds,
    _channel_privacy,
    _collect_events,
    collect_once,
    collect_with_offline,
    sync_queue,
)
from clients.collectors.queue import CollectorQueue
from clients.collectors.resource_probe import summarize
from clients.collectors.screen import ScreenState


@pytest.fixture(autouse=True)
def _isolate_collector_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("EV_AUDIO_SCENE_FILE", str(tmp_path / "audio-scene.json"))
    monkeypatch.setenv("EV_LOCATION_FILE", str(tmp_path / "location.json"))
    for name in (
        "EV_AUDIO_SCENE",
        "EV_IN_CALL",
        "EV_AUDIO_CONFIDENCE",
        "EV_LOCATION_PLACE",
        "EV_LOCATION_PRESENCE",
        "EV_LIVE_PRIVACY",
        "EV_SCREEN_PRIVACY",
        "EV_AUDIO_PRIVACY",
        "EV_LOCATION_PRIVACY",
        "EV_SCREEN_CHANNEL_ID",
        "EV_AUDIO_CHANNEL_ID",
        "EV_LOCATION_CHANNEL_ID",
        "EV_SCREEN_NATIVE",
        "EV_SCREEN_OCR",
        "EV_SCREEN_CAPTURE_CMD",
        "EV_LOCATION_NATIVE",
        "EV_AUDIO_SAMPLE_FILE",
        "EV_COLLECTOR_QUEUE_DIR",
        "EV_COLLECTOR_QUEUE_MAX_RECORDS",
        "EV_COLLECTOR_QUEUE_MAX_BYTES",
        "EV_COLLECTOR_HELPER_DIR",
        "EV_COLLECTOR_HELPER_BIN",
        "EV_LOCATION_PLACES_FILE",
        "EV_DEVICE_ID",
    ):
        monkeypatch.delenv(name, raising=False)


def _runner(app: str = "Xcode", window: str = "retrieval.py — EV") -> object:
    def runner(command: str) -> str:
        if "get name of first application process" in command:
            return app
        if "get name of first window" in command:
            return window
        return ""

    return runner


def _native_runner() -> object:
    def runner(command: str) -> str:
        if command == "ambient screen json":
            return json.dumps(
                {
                    "app_name": "Google Chrome",
                    "bundle_id": "com.google.Chrome",
                    "window_title": "EV docs — Google Chrome",
                    "category": "browser",
                    "url": "https://example.com/ev-docs",
                    "idle_seconds": 12.5,
                }
            )
        return ""

    return runner


async def _collect(payloads: list[dict]) -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(201, json=[])

    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_screen_collector_emits_derived_text_only() -> None:
    payloads: list[dict] = []
    client = await _collect(payloads)
    try:
        counts = await collect_once(client, screen_runner=_runner())  # type: ignore[arg-type]
    finally:
        await client.aclose()

    assert counts == {"screen-activity": 1}
    body = payloads[0]
    assert body["channel"] == "screen-activity"
    assert body["kind"] == "screen"
    assert body["privacy_level"] == "sensitive"
    screen = body["events"][0]["payload"]
    assert screen["app"] == "Xcode"
    assert screen["document"] == "retrieval.py — EV"
    assert screen["code_file"] == "retrieval.py"
    assert "raw" not in json.dumps(screen)


async def test_audio_and_location_hints_from_env_are_minimal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EV_AUDIO_SCENE", "meeting")
    monkeypatch.setenv("EV_IN_CALL", "1")
    monkeypatch.setenv("EV_AUDIO_CONFIDENCE", "0.93")
    monkeypatch.setenv("EV_LOCATION_PLACE", "Bengaluru Airport")
    monkeypatch.setenv("EV_LOCATION_PRESENCE", "present")
    monkeypatch.setenv("EV_DEVICE_ID", "mac-collector")

    payloads: list[dict] = []
    client = await _collect(payloads)
    try:
        counts = await collect_once(client, screen_runner=_runner(app="", window=""))  # type: ignore[arg-type]
    finally:
        await client.aclose()

    assert counts == {"audio-ambient": 1, "location-coarse": 1}
    by_channel = {body["channel"]: body for body in payloads}
    audio = by_channel["audio-ambient"]
    assert audio["kind"] == "audio"
    assert audio["privacy_level"] == "sensitive"
    assert audio["events"][0]["payload"] == {
        "scene": "meeting",
        "in_call": True,
        "confidence": 0.93,
    }
    assert "transcript" not in json.dumps(audio)

    location = by_channel["location-coarse"]
    assert location["kind"] == "location"
    assert location["privacy_level"] == "private"
    assert location["events"][0]["payload"] == {
        "place": "Bengaluru Airport",
        "presence": "present",
    }
    assert "latitude" not in json.dumps(location)
    assert location["events"][0]["device_id"] == "mac-collector"


async def test_collector_uses_channel_endpoint_when_channel_id_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EV_SCREEN_CHANNEL_ID", "ch-screen-1")
    monkeypatch.setenv("EV_AUDIO_CHANNEL_ID", "ch-audio-1")
    monkeypatch.setenv("EV_AUDIO_SCENE", "music")
    monkeypatch.setenv("EV_LOCATION_CHANNEL_ID", "ch-location-1")
    monkeypatch.setenv("EV_LOCATION_PRESENCE", "home")

    requests: list[str] = []
    payloads: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        payloads.append(json.loads(request.content))
        return httpx.Response(201, json=[])

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    try:
        counts = await collect_once(client, screen_runner=_runner())  # type: ignore[arg-type]
    finally:
        await client.aclose()

    assert counts == {"screen-activity": 1, "audio-ambient": 1, "location-coarse": 1}
    assert "/v1/live/channels/ch-screen-1/events" in requests[0]
    assert "/v1/live/channels/ch-audio-1/events" in requests[1]
    assert "/v1/live/channels/ch-location-1/events" in requests[2]
    # Channel-endpoint payloads are bare event lists, not batch envelopes.
    assert isinstance(payloads[0], list)
    assert payloads[0][0]["event_type"] == "focus_change"
    assert "channel" not in payloads[0][0]
    assert "privacy_level" not in payloads[0][0]


async def test_per_channel_privacy_defaults() -> None:
    assert _channel_privacy("screen-activity") == "sensitive"
    assert _channel_privacy("audio-ambient") == "sensitive"
    assert _channel_privacy("location-coarse") == "private"


async def test_per_channel_privacy_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EV_LOCATION_PRIVACY", "never_send_to_model")
    assert _channel_privacy("location-coarse") == "never_send_to_model"


async def test_audio_scene_from_local_hint_file(tmp_path) -> None:
    hint = tmp_path / "audio-scene.json"
    hint.write_text(json.dumps({"scene": "music", "in_call": False, "confidence": 0.88}))
    events = _collect_events()
    scene = events["audio-ambient"][0]["payload"]
    assert scene == {"scene": "music", "in_call": False, "confidence": 0.88}


async def test_no_signals_skips_post() -> None:
    payloads: list[dict] = []
    client = await _collect(payloads)
    try:
        counts = await collect_once(client, screen_runner=_runner(app="", window=""))  # type: ignore[arg-type]
    finally:
        await client.aclose()
    assert counts == {}
    assert payloads == []


async def test_screen_failure_degrades_without_raw_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EV_AUDIO_SCENE", "meeting")

    def broken_runner(command: str) -> str:
        raise RuntimeError("no accessibility permission")

    payloads: list[dict] = []
    client = await _collect(payloads)
    try:
        counts = await collect_once(client, screen_runner=broken_runner)  # type: ignore[arg-type]
    finally:
        await client.aclose()

    assert counts == {"audio-ambient": 1}
    assert payloads[0]["channel"] == "audio-ambient"


async def test_collector_end_to_end_feeds_live_state(
    client: AsyncClient,
    db_session,
) -> None:
    counts = await collect_once(client, screen_runner=_runner())  # type: ignore[arg-type]
    assert counts == {"screen-activity": 1}

    # The user slice sees the derived screen context...
    state = (await client.get("/v1/state")).json()
    lines = [line for line in state["live_context"] if "screen-activity" in line]
    assert lines, state["live_context"]
    assert "app=Xcode" in lines[0]
    assert "code_file=retrieval.py" in lines[0]
    # The active code file becomes the derived current task when no newer
    # conversational event supersedes it (user slice only).
    assert state["current_task"] == "Xcode: retrieval.py"

    # ...but the model slice never receives sensitive screen events by default.
    model_state = await build_user_state(db_session, access="model")
    model_lines = [line for line in model_state.live_context if "screen-activity" in line]
    assert model_lines == []
    assert model_state.current_task != "Xcode: retrieval.py"


async def test_screen_collector_fires_routine_trigger(
    client: AsyncClient,
    db_session,
) -> None:
    resp = await client.post(
        "/v1/routines",
        json={
            "name": "xcode-focus-card",
            "kind": "trigger",
            "trigger": {
                "channel_kind": "screen",
                "event_type": "focus_change",
                "conditions": [{"path": "app", "op": "eq", "value": "Xcode"}],
            },
            "action_type": "hud_card",
            "action_payload": {"card": "focus"},
        },
    )
    assert resp.status_code == 201, resp.text

    counts = await collect_once(client, screen_runner=_runner())  # type: ignore[arg-type]
    assert counts == {"screen-activity": 1}

    runs = await list_runs(db_session)
    assert runs, "screen collector event did not trigger the routine"
    assert runs[0].kind == "trigger"
    assert runs[0].status == "executed"
    assert runs[0].trigger_snapshot["channel_kind"] == "screen"
    assert runs[0].trigger_snapshot["payload"]["app"] == "Xcode"


def test_cli_collect_parser() -> None:
    from clients.cli import build_parser

    args = build_parser().parse_args(["collect", "--once", "--interval", "5"])
    assert args.command == "collect"
    assert args.once is True
    assert args.interval == 5


async def test_cli_collect_dispatches_to_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    from clients.cli import _run, build_parser

    calls: list[tuple[int, bool]] = []

    async def fake_run_agent(*, interval_seconds: int, once: bool) -> None:
        calls.append((interval_seconds, once))

    monkeypatch.setattr(collector_agent, "run_agent", fake_run_agent)
    args = build_parser().parse_args(["collect", "--once"])
    assert await _run(args) == 0
    assert calls == [(30, True)]


def test_cli_vision_parser() -> None:
    from clients.cli import build_parser

    args = build_parser().parse_args(["vision", "confirm", "rec-1", "--type", "person"])
    assert args.command == "vision"
    assert args.vision_command == "confirm"
    assert args.recognition_id == "rec-1"
    assert args.type == "person"

    args = build_parser().parse_args(["vision", "list", "--limit", "10"])
    assert args.vision_command == "list"
    assert args.limit == 10


async def test_cli_vision_confirm_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    from clients.cli import _run, build_parser

    monkeypatch.setenv("EV_API_KEY", "test-key")
    calls: list[tuple[str, str]] = []

    async def fake_confirm(client, recognition_id: str, *, entity_type: str) -> dict:
        calls.append((recognition_id, entity_type))
        return {"label": "Maya", "source": "user", "entity_id": "e1"}

    monkeypatch.setattr(cli_vision, "confirm_recognition", fake_confirm)
    args = build_parser().parse_args(["vision", "confirm", "rec-1", "--type", "person"])
    assert await _run(args) == 0
    assert calls == [("rec-1", "person")]


async def test_screen_native_payload_has_rich_derived_fields() -> None:
    events = _collect_events(screen_runner=_native_runner())  # type: ignore[arg-type]
    screen = events["screen-activity"][0]["payload"]
    assert screen["app"] == "Google Chrome"
    assert screen["category"] == "browser"
    assert screen["url"] == "https://example.com/ev-docs"
    assert screen["idle_seconds"] == 12.5
    assert isinstance(screen["focus_seconds"], int)
    assert "raw" not in json.dumps(screen)


def test_focus_tracker_measures_continuous_session() -> None:
    clock = iter([100.0, 110.0, 111.0])
    tracker = FocusTracker(now=lambda: next(clock))
    first = ScreenState(app="Xcode", document="a.py")
    assert tracker.focus_seconds(first) == 0.0
    assert tracker.focus_seconds(first) == 10.0
    switched = ScreenState(app="Notes", document=None)
    assert tracker.focus_seconds(switched) == 0.0


async def test_screen_ocr_uses_agent6_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    class _FakeAgent6Provider:
        name = "deterministic"

        async def analyze(self, *, data, content_type=None, filename=None, prompt=None):
            return SimpleNamespace(ocr_text="spider-lab sprint notes")

    monkeypatch.setattr(
        "app.vision.providers.get_vision_provider",
        lambda: _FakeAgent6Provider(),
    )
    monkeypatch.setenv("EV_SCREEN_OCR", "1")
    monkeypatch.setenv("EV_SCREEN_CAPTURE_CMD", "printf 'spider-lab sprint notes'")
    events = _collect_events(screen_runner=_native_runner())  # type: ignore[arg-type]
    screen = events["screen-activity"][0]["payload"]
    assert screen["summary"] == "spider-lab sprint notes"
    # Only derived text is emitted; no image bytes in the payload.
    assert "spider-lab sprint notes" in json.dumps(screen)
    assert "PNG" not in json.dumps(screen)


def test_offline_queue_is_bounded_fifo(tmp_path) -> None:
    queue = CollectorQueue(queue_dir=tmp_path, max_records=3, max_bytes=1_000_000)
    for index in range(5):
        queue.enqueue(
            channel=f"channel-{index}",
            kind="screen",
            events=[{"event_type": "focus_change", "payload": {"app": f"App{index}"}}],
            channel_id=None,
            privacy_level="sensitive",
        )
    records = queue.records()
    assert len(records) == 3
    assert [record["channel"] for record in records] == ["channel-2", "channel-3", "channel-4"]
    assert all(record["idempotency_key"] for record in records)


async def test_collect_with_offline_queues_then_replays_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("EV_AUDIO_SCENE", "meeting")
    queue = CollectorQueue(queue_dir=tmp_path / "queue")

    async def offline_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated outage", request=request)

    offline = httpx.AsyncClient(
        transport=httpx.MockTransport(offline_handler),
        base_url="http://test",
    )
    try:
        result = await collect_with_offline(offline, queue, screen_runner=_runner())  # type: ignore[arg-type]
    finally:
        await offline.aclose()

    assert result["posted"] == {}
    assert result["queued"] == {"screen-activity": 1, "audio-ambient": 1}
    assert result["failed"] is True
    assert len(queue) == 2

    delivered: list[tuple[str, dict, str | None]] = []

    async def online_handler(request: httpx.Request) -> httpx.Response:
        delivered.append(
            (
                str(request.url),
                json.loads(request.content),
                request.headers.get("Idempotency-Key"),
            )
        )
        return httpx.Response(201, json=[])

    online = httpx.AsyncClient(
        transport=httpx.MockTransport(online_handler),
        base_url="http://test",
    )
    try:
        summary = await sync_queue(online, queue)
    finally:
        await online.aclose()

    assert summary == {"synced": 2, "quarantined": 0, "remaining": 0, "failed": False}
    assert len(delivered) == 2
    by_channel = {body["channel"] for _, body, _ in delivered}
    assert by_channel == {"screen-activity", "audio-ambient"}
    assert all(key and key.startswith("collector-") for _, _, key in delivered)


async def test_collect_with_offline_rejects_4xx_without_queueing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("EV_AUDIO_SCENE", "meeting")
    queue = CollectorQueue(queue_dir=tmp_path / "queue")

    async def bad_request_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "bad channel id"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(bad_request_handler),
        base_url="http://test",
    )
    try:
        result = await collect_with_offline(client, queue, screen_runner=_runner())  # type: ignore[arg-type]
    finally:
        await client.aclose()

    assert result["posted"] == {}
    assert result["queued"] == {}
    assert result["rejected"] == {"screen-activity": 1, "audio-ambient": 1}
    assert result["failed"] is False
    assert len(queue) == 0


async def test_sync_queue_quarantines_malformed_records(tmp_path) -> None:
    queue = CollectorQueue(queue_dir=tmp_path / "queue")
    queue.enqueue(
        channel="screen-activity",
        kind="screen",
        events=[{"event_type": "focus_change", "payload": {"app": "Xcode"}}],
        channel_id=None,
        privacy_level="sensitive",
    )
    # Inject one corrupt line so replay must quarantine it and continue.
    with queue.path.open("a", encoding="utf-8") as handle:
        handle.write("{not-json}\n")

    delivered: list[dict] = []

    async def ok_handler(request: httpx.Request) -> httpx.Response:
        delivered.append(json.loads(request.content))
        return httpx.Response(201, json=[])

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(ok_handler),
        base_url="http://test",
    )
    try:
        summary = await sync_queue(client, queue)
    finally:
        await client.aclose()

    assert summary == {"synced": 1, "quarantined": 1, "remaining": 0, "failed": False}
    assert len(delivered) == 1
    assert delivered[0]["channel"] == "screen-activity"


def test_backoff_is_exponential_and_capped() -> None:
    assert _backoff_seconds(30, 0) == 30
    assert _backoff_seconds(30, 1) == 60
    assert _backoff_seconds(30, 2) == 120
    assert _backoff_seconds(30, 10) == 600


def test_resource_probe_summarize_computes_curve_numbers(tmp_path) -> None:
    path = tmp_path / "resource.jsonl"
    path.write_text(
        "\n".join(
            [
                '{"alive": true, "cpu_percent": 0.0, "pid": 1, "queue_count": 2, "rss_mb": 4.0, "ts": "t1"}',
                '{"alive": true, "cpu_percent": 2.0, "pid": 1, "queue_count": 9, "rss_mb": 8.0, "ts": "t2"}',
                '{"alive": true, "cpu_percent": 1.0, "pid": 1, "queue_count": 9, "rss_mb": 6.0, "ts": "t3"}',
                "{corrupt}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    report = summarize(path)
    assert report["rows"] == 3
    assert report["alive_rows"] == 3
    assert report["rss_min_mb"] == 4.0
    assert report["rss_max_mb"] == 8.0
    assert report["rss_avg_mb"] == 6.0
    assert report["cpu_max"] == 2.0
    assert report["cpu_avg"] == 1.0
    assert report["queue_max"] == 9
    assert report["start_ts"] == "t1"
    assert report["end_ts"] == "t3"


def test_native_helper_is_a_noop_on_non_darwin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_native.sys, "platform", "linux")
    assert _native.helper_binary() is None
    assert _native.run_helper(["--screen"]) is None


def test_location_native_noops_on_non_darwin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EV_LOCATION_NATIVE", "1")
    monkeypatch.setattr(location_module.sys, "platform", "linux")
    assert location_module.location_context() is None


def test_screen_collector_noops_when_os_query_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_osascript(command: str) -> str:
        raise FileNotFoundError("osascript not found (non-Darwin host)")

    monkeypatch.setattr(screen_module, "_default_runner", missing_osascript)
    assert screen_module.screen_state(None).to_payload() == {}


async def test_offline_queue_replay_after_simulated_hour_is_duplicate_free(
    client: AsyncClient,
    db_session,
    tmp_path,
) -> None:
    """A queue record backdated by one hour replays without duplicates."""
    queue = CollectorQueue(queue_dir=tmp_path / "queue")
    queue.enqueue(
        channel="screen-activity",
        kind="screen",
        events=[{"event_type": "focus_change", "payload": {"app": "Xcode"}}],
        channel_id=None,
        privacy_level="sensitive",
    )
    records = queue.records()
    assert len(records) == 1
    records[0]["queued_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    queue.path.write_text(json.dumps(records[0], sort_keys=True) + "\n", encoding="utf-8")

    summary = await sync_queue(client, queue)
    assert summary["synced"] == 1
    assert summary["remaining"] == 0

    rows = (await db_session.execute(select(LiveEvent))).scalars().all()
    assert len(rows) == 1

    # A second replay of the same logical batch (e.g. queue restored from disk
    # after the outage) must not duplicate the stored event.
    queue.path.write_text(json.dumps(records[0], sort_keys=True) + "\n", encoding="utf-8")
    summary_again = await sync_queue(client, queue)
    assert summary_again["synced"] == 1
    rows = (await db_session.execute(select(LiveEvent))).scalars().all()
    assert len(rows) == 1
    assert rows[0].payload["app"] == "Xcode"


def test_location_native_permission_surfaced_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EV_LOCATION_NATIVE", "1")
    monkeypatch.setattr(
        location_module,
        "_native_location",
        lambda: {
            "authorization_status": "denied",
            "location_available": False,
            "presence": "unknown",
        },
    )
    location_module.reset_permission_state()
    assert location_module.location_context() == {"presence": "unknown", "permission": "denied"}
    # Suppressed until the status changes, so the stream is not spammed.
    assert location_module.location_context() is None
    location_module.reset_permission_state()
    assert location_module.location_context() == {"presence": "unknown", "permission": "denied"}


def test_location_native_coarse_presence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EV_LOCATION_NATIVE", "1")
    monkeypatch.setattr(
        location_module,
        "_native_location",
        lambda: {
            "authorization_status": "authorizedWhenInUse",
            "location_available": True,
            "place": "Home",
            "presence": "home",
        },
    )
    assert location_module.location_context() == {"place": "Home", "presence": "home"}
