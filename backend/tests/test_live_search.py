"""Keyless live search: Open-Meteo weather + provider selection."""

from __future__ import annotations

from app.config import settings
from app.search import live
from app.search.live import (
    LiveSearchProvider,
    extract_place,
    is_weather_query,
    needs_live_lookup,
    weather_results,
)
from app.search.providers import get_search_provider


def test_weather_query_detection() -> None:
    assert is_weather_query("what's the weather")
    assert is_weather_query("Will it rain tomorrow in Surat")
    assert needs_live_lookup("what's the status of the weather")
    assert needs_live_lookup("what is the capital of France")
    assert needs_live_lookup("look up the Eiffel Tower")
    assert not is_weather_query("remind me to stretch")
    assert not needs_live_lookup("what's on my calendar today")
    assert not needs_live_lookup("what's 25 * 4?")
    assert not needs_live_lookup("what time is it")


def test_extract_place_from_weather_question() -> None:
    assert extract_place("weather in London") == "London"
    assert extract_place("What's the weather in Surat today?") == "Surat"
    assert extract_place("what's the weather") is None


def test_live_provider_is_selected(monkeypatch) -> None:
    monkeypatch.setattr(settings, "search_provider", "live")
    provider = get_search_provider()
    assert isinstance(provider, LiveSearchProvider)


async def test_weather_results_maps_open_meteo(monkeypatch) -> None:
    async def fake_place(_query: str) -> str:
        return "Surat"

    monkeypatch.setattr(live, "resolve_place", fake_place)
    captured: list[str] = []

    class _FakeResponse:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    class _FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def get(self, url: str, **kwargs) -> _FakeResponse:
            captured.append(url)
            if "geocoding" in url:
                return _FakeResponse(
                    {
                        "results": [
                            {
                                "name": "Surat",
                                "admin1": "Gujarat",
                                "country": "India",
                                "latitude": 21.17,
                                "longitude": 72.83,
                                "timezone": "Asia/Kolkata",
                            }
                        ]
                    }
                )
            return _FakeResponse(
                {
                    "current": {
                        "temperature_2m": 31.2,
                        "apparent_temperature": 34.0,
                        "relative_humidity_2m": 70,
                        "weather_code": 1,
                        "wind_speed_10m": 12.4,
                    },
                    "daily": {
                        "time": ["2026-08-14", "2026-08-15"],
                        "weather_code": [1, 2],
                        "temperature_2m_max": [33.0, 34.0],
                        "temperature_2m_min": [26.0, 27.0],
                    },
                }
            )

    monkeypatch.setattr(live.httpx, "AsyncClient", _FakeClient)
    results = await weather_results("what's the weather", limit=1)
    assert len(results) == 1
    assert "Surat" in results[0].title
    assert "31.2" in results[0].snippet
    assert "mainly clear" in results[0].snippet
    assert any("open-meteo.com/v1/forecast" in url for url in captured)


async def test_get_weather_tool_dispatch(client, monkeypatch) -> None:
    async def fake_weather(query: str, *, limit: int = 3):
        return [
            type(
                "R",
                (),
                {
                    "title": "Weather in Surat",
                    "url": "https://open-meteo.com/",
                    "snippet": "Surat: mainly clear. 31°C",
                },
            )()
        ]

    monkeypatch.setattr("app.search.live.weather_results", fake_weather)
    resp = await client.post(
        "/v1/gateway/tools",
        json={"name": "get_weather", "arguments": {"place": "Surat"}},
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["ok"] is True
    assert payload["result"]["count"] == 1
    assert "Surat" in payload["result"]["results"][0]["title"]
