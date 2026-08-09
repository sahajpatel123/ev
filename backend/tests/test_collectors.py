"""Tests for the privacy-preserving OS-level perception collectors."""

from __future__ import annotations

import json

import httpx
import pytest
from httpx import AsyncClient

from app.ev.user_state import build_user_state
from clients.collectors.agent import _collect_events, collect_once


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
    assert body["privacy_level"] == "normal"
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
    monkeypatch.setenv("EV_LIVE_PRIVACY", "sensitive")
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
    assert location["events"][0]["payload"] == {
        "place": "Bengaluru Airport",
        "presence": "present",
    }
    assert "latitude" not in json.dumps(location)
    assert location["events"][0]["device_id"] == "mac-collector"


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

    state = (await client.get("/v1/state")).json()
    lines = [line for line in state["live_context"] if "screen-activity" in line]
    assert lines, state["live_context"]
    assert "app=Xcode" in lines[0]
    assert "code_file=retrieval.py" in lines[0]

    model_state = await build_user_state(db_session, access="model")
    model_lines = [line for line in model_state.live_context if "screen-activity" in line]
    assert model_lines
    assert "app=Xcode" in model_lines[0]
