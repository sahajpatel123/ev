"""Keyless live lookup: Open-Meteo weather plus Wikipedia/DDG citations.

Brave Search needs a paid key this host does not have. Weather questions
must still return a real forecast, so this provider uses public APIs that
require no credential. Non-weather queries fall back to Wikipedia / DuckDuckGo
Instant Answer, and to Brave when ``EV_BRAVE_SEARCH_API_KEY`` is later set.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx

from app.config import settings


def _result(title: str, url: str, snippet: str):
    from app.search.providers import SearchResult

    return SearchResult(title=title, url=url, snippet=snippet)

_WEATHER_RE = re.compile(
    r"\b(weather|forecast|temperature|temps?\b|rain|snow|humid|uv\s*index|"
    r"air quality|aqi|wind\s*speed|how hot|how cold|degrees)\b",
    re.IGNORECASE,
)
_PLACE_RE = re.compile(
    r"(?:weather|forecast|temperature|rain|snow|aqi|air quality)"
    r"\s+(?:in|for|at|near)\s+(.+)$"
    r"|(?:in|for|at|near)\s+([A-Za-z][A-Za-z .,'-]{1,60})"
    r"\s+(?:weather|forecast|temperature)",
    re.IGNORECASE,
)
_USER_AGENT = "EV-personal-assistant/1.0 (local; weather+search)"
_WMO = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "rime fog",
    51: "light drizzle",
    53: "drizzle",
    55: "dense drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    80: "rain showers",
    81: "rain showers",
    82: "heavy rain showers",
    95: "thunderstorm",
    96: "thunderstorm with hail",
    99: "thunderstorm with hail",
}


_ARITH_RE = re.compile(r"\d+\s*[\+\-\*/%^]\s*\d|\b\d[\d,]*\s*%\s+of\b")
_PERSONAL_RE = re.compile(
    r"\b(my|mine|i'm|i've|i'd|i'll|\bi\b|me|our|ours)\b",
    re.IGNORECASE,
)
_WORLD_HINTS = (
    "search the web",
    "look this up",
    "look it up",
    "look up",
    "google ",
    "wikipedia",
    "headline",
    "stock price",
    "who won",
    "capital of",
    "population of",
    "latest news",
    "current events",
    "define ",
    "meaning of",
    "who invented",
    "who founded",
    "when was ",
    "when were ",
    "where is the ",
    "where are the ",
    "who is the ",
    "who was the ",
)


def is_weather_query(text: str) -> bool:
    return bool(_WEATHER_RE.search(text or ""))


def looks_world_knowledge(text: str) -> bool:
    """Public-fact lookup — not personal memory, math, or small talk."""

    raw = text or ""
    if not raw.strip() or _ARITH_RE.search(raw) or _PERSONAL_RE.search(raw):
        return False
    if is_weather_query(raw):
        return False
    if re.search(
        r"\b(?:what(?:'s| is) the (?:time|date)|what time is it|what day is it)\b",
        raw,
        re.IGNORECASE,
    ):
        return False
    lowered = raw.lower()
    return any(hint in lowered for hint in _WORLD_HINTS)


def needs_live_lookup(text: str) -> bool:
    if is_weather_query(text or ""):
        return True
    return looks_world_knowledge(text or "")


def extract_place(text: str) -> str | None:
    match = _PLACE_RE.search(text or "")
    if not match:
        return None
    place = (match.group(1) or match.group(2) or "").strip(" ?.,!")
    place = re.sub(r"\b(today|tomorrow|tonight|please|right now)\b", "", place, flags=re.I)
    place = re.sub(r"\s+", " ", place).strip(" ?.,!")
    return place or None


def default_place() -> str | None:
    """Coarse place: env, ~/.ev/location.json, then nothing (caller may IP-geo)."""

    configured = (settings.location_place or "").strip()
    if configured:
        return configured[:80]
    path = Path(getattr(settings, "location_file", None) or "~/.ev/location.json").expanduser()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    place = str(data.get("place") or data.get("coarse_place") or "").strip()
    return place[:80] or None


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=settings.search_timeout_seconds,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
    )


async def _ip_place() -> str | None:
    urls = (
        "https://wttr.in/?format=%l",
        "https://ipinfo.io/json",
        "https://ipapi.co/json/",
    )
    async with _client() as client:
        for url in urls:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
            except (httpx.HTTPError, ValueError):
                continue
            if "wttr.in" in url:
                line = resp.text.strip()
                if line and "not found" not in line.lower():
                    return line.split(",")[0].strip()[:80] or None
                continue
            try:
                payload = resp.json()
            except ValueError:
                continue
            city = str(payload.get("city") or "").strip()
            region = str(payload.get("region") or payload.get("regionName") or "").strip()
            if city:
                return f"{city}, {region}".strip(", ")[:80] if region else city[:80]
    return None


async def resolve_place(text: str) -> str | None:
    return extract_place(text) or default_place() or await _ip_place()


async def geocode(place: str) -> dict | None:
    async with _client() as client:
        try:
            resp = await client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": place, "count": 1, "language": "en", "format": "json"},
            )
            resp.raise_for_status()
            results = (resp.json() or {}).get("results") or []
        except (httpx.HTTPError, ValueError):
            return None
    if not results:
        return None
    row = results[0]
    return {
        "name": row.get("name") or place,
        "admin": row.get("admin1") or "",
        "country": row.get("country") or "",
        "latitude": row.get("latitude"),
        "longitude": row.get("longitude"),
        "timezone": row.get("timezone") or "auto",
    }


def _describe_code(code: object) -> str:
    if not isinstance(code, (int, float, str)):
        return "unknown conditions"
    try:
        return _WMO.get(int(code), f"code {code}")
    except (TypeError, ValueError):
        return "unknown conditions"


def _format_forecast(geo: dict, data: dict) -> str:
    label = ", ".join(part for part in (geo.get("name"), geo.get("admin"), geo.get("country")) if part)
    current = data.get("current") or {}
    daily = data.get("daily") or {}
    temp = current.get("temperature_2m")
    apparent = current.get("apparent_temperature")
    wind = current.get("wind_speed_10m")
    humidity = current.get("relative_humidity_2m")
    sky = _describe_code(current.get("weather_code"))
    parts = [f"{label}: {sky}"]
    if temp is not None:
        feels = f" (feels {apparent}°C)" if apparent is not None else ""
        parts.append(f"{temp}°C{feels}")
    if humidity is not None:
        parts.append(f"humidity {humidity}%")
    if wind is not None:
        parts.append(f"wind {wind} km/h")
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    codes = daily.get("weather_code") or []
    dates = daily.get("time") or []
    days = []
    for index, date in enumerate(dates[:3]):
        high = highs[index] if index < len(highs) else "?"
        low = lows[index] if index < len(lows) else "?"
        sky_day = _describe_code(codes[index] if index < len(codes) else None)
        days.append(f"{date} {sky_day}, high {high}°C / low {low}°C")
    snippet = ". ".join(parts)
    if days:
        snippet += ". Next days: " + "; ".join(days)
    return snippet


async def weather_results(query: str, *, limit: int = 3) -> list:
    place = await resolve_place(query)
    if not place:
        return [
            _result(
                "Weather location needed",
                "https://open-meteo.com/",
                (
                    "No coarse place is configured. Ask 'weather in <city>' "
                    "or set EV_LOCATION_PLACE."
                ),
            )
        ]
    geo = await geocode(place)
    if geo is None or geo.get("latitude") is None:
        return [
            _result(
                f"Could not geocode {place}",
                "https://open-meteo.com/",
                f"Open-Meteo geocoding returned no match for {place!r}.",
            )
        ]
    async with _client() as client:
        try:
            resp = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": geo["latitude"],
                    "longitude": geo["longitude"],
                    "current": (
                        "temperature_2m,relative_humidity_2m,apparent_temperature,"
                        "weather_code,wind_speed_10m"
                    ),
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min",
                    "timezone": geo.get("timezone") or "auto",
                    "forecast_days": 3,
                },
            )
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            return [
                _result(
                    f"Weather fetch failed for {geo['name']}",
                    "https://open-meteo.com/",
                    str(exc)[:240],
                )
            ]
    snippet = _format_forecast(geo, payload)
    lat, lon = geo["latitude"], geo["longitude"]
    return [
        _result(
            f"Weather in {geo['name']}",
            f"https://open-meteo.com/en/docs#latitude={lat}&longitude={lon}",
            snippet,
        )
    ][: max(1, min(int(limit), 10))]


async def wikipedia_results(query: str, *, limit: int = 5) -> list:
    async with _client() as client:
        try:
            resp = await client.get(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "opensearch",
                    "search": query,
                    "limit": max(1, min(int(limit), 10)),
                    "namespace": 0,
                    "format": "json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError, TypeError):
            return []
    titles = data[1] if isinstance(data, list) and len(data) > 1 else []
    snippets = data[2] if isinstance(data, list) and len(data) > 2 else []
    urls = data[3] if isinstance(data, list) and len(data) > 3 else []
    results = []
    for index, title in enumerate(titles):
        results.append(
            _result(
                str(title),
                str(urls[index] if index < len(urls) else "https://en.wikipedia.org/"),
                str(snippets[index] if index < len(snippets) else title),
            )
        )
    return results


async def duckduckgo_results(query: str) -> list:
    async with _client() as client:
        try:
            resp = await client.get(
                "https://api.duckduckgo.com/",
                params={
                    "q": query,
                    "format": "json",
                    "no_html": 1,
                    "skip_disambig": 1,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            return []
    abstract = str(data.get("AbstractText") or "").strip()
    url = str(data.get("AbstractURL") or data.get("AbstractSource") or "").strip()
    heading = str(data.get("Heading") or query).strip()
    if not abstract:
        return []
    return [
        _result(
            heading or "DuckDuckGo",
            url or "https://duckduckgo.com/",
            abstract[:500],
        )
    ]


class LiveSearchProvider:
    """No-key live lookup. Weather always works; other queries use public APIs."""

    name = "live"

    async def search(self, query: str, *, limit: int = 5) -> list:
        limit = max(1, min(int(limit), 10))
        if is_weather_query(query):
            return await weather_results(query, limit=limit)
        if settings.brave_search_api_key:
            from app.search.providers import BraveSearchProvider

            return await BraveSearchProvider(
                api_key=settings.brave_search_api_key,
                base_url=settings.brave_search_base_url,
                timeout_seconds=settings.search_timeout_seconds,
            ).search(query, limit=limit)
        results = await duckduckgo_results(query)
        if len(results) < limit:
            results.extend(await wikipedia_results(query, limit=limit - len(results)))
        return results[:limit]


async def live_grounding_text(message: str) -> str | None:
    """Prefetch cited live facts so voice/chat need not wait on tool JSON."""

    if not needs_live_lookup(message):
        return None
    from app.search.providers import get_search_provider

    provider = get_search_provider()
    if provider is None:
        return None
    try:
        results = await provider.search(message, limit=3)
    except Exception:  # noqa: BLE001 - grounding is best-effort
        return None
    if not results:
        return None
    lines = []
    for result in results:
        lines.append(f"- {result.title}: {result.snippet} ({result.url})")
    return "\n".join(lines)
