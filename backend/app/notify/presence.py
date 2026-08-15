"""EVIE presence overlay — open native HUD windows on the owner's Mac.

The product surface is not a localhost dashboard. When EVIE has something to
show, she opens one or more HUD windows via ``ev://present``. Delivery is
honest: if the SUIT app is not installed or the URL scheme is unregistered,
this tries an independent ``/app/lookout`` visor window and otherwise returns
``opened=False`` with the exact next step — never a fake success.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from app.config import settings
from app.ev.lookout import SurfacePlan, SurfaceWindow, make_window, plan_surfaces, window_from_args
from app.notify import lookouts as lookout_registry


def _base_web() -> str:
    raw = os.environ.get("EV_API_URL") or getattr(settings, "api_url", "") or "http://127.0.0.1:8000"
    return str(raw).rstrip("/")


def present_url(
    *,
    title: str,
    body: str,
    kind: str = "card",
    size: str | None = None,
    time_type: str | None = None,
    placement: str | None = None,
    ttl_ms: int | None = None,
    items: Iterable[str] | None = None,
    recommendation: str | None = None,
    source: str | None = None,
    window_id: str | None = None,
    lookout: bool = False,
    lat: float | None = None,
    lon: float | None = None,
    dest_lat: float | None = None,
    dest_lon: float | None = None,
    questions: Iterable[str] | None = None,
    response: str | None = None,
    layout: str | None = None,
    drift_x: int | None = None,
    drift_y: int | None = None,
    tilt: float | None = None,
) -> str:
    query: dict[str, str] = {
        "title": title[:120],
        "body": body[:4000],
        "kind": (kind or "card")[:32],
    }
    if size:
        query["size"] = str(size)[:16]
    if time_type:
        query["time"] = str(time_type)[:16]
    if placement:
        query["place"] = str(placement)[:16]
    if ttl_ms is not None:
        query["ttl"] = str(int(ttl_ms))
    if window_id:
        query["id"] = str(window_id)[:64]
    if lookout:
        query["lookout"] = "1"
    packed_items = [str(item) for item in (items or []) if str(item).strip()]
    if packed_items:
        query["items"] = "|".join(packed_items)[:1500]
    packed_questions = [str(item) for item in (questions or []) if str(item).strip()]
    if packed_questions:
        query["questions"] = "|".join(packed_questions)[:1500]
    if recommendation:
        query["recommendation"] = recommendation[:400]
    if source:
        query["source"] = source[:160]
    if response:
        query["response"] = response[:4000]
    if layout:
        query["layout"] = str(layout)[:16]
    if drift_x is not None:
        query["dx"] = str(int(drift_x))
    if drift_y is not None:
        query["dy"] = str(int(drift_y))
    if tilt is not None:
        query["tilt"] = f"{float(tilt):.2f}"
    if lat is not None:
        query["lat"] = str(lat)
    if lon is not None:
        query["lon"] = str(lon)
    if dest_lat is not None:
        query["dest_lat"] = str(dest_lat)
    if dest_lon is not None:
        query["dest_lon"] = str(dest_lon)
    # quote (not quote_plus): spaces become %20. Swift URLComponents
    # leaves "+" literal, which painted "No+update+from+him" on the HUD.
    return f"ev://present?{urlencode(query, quote_via=quote)}"


def lookout_web_url(window: SurfaceWindow) -> str:
    query = urlencode(
        {
            "id": window.id,
            "title": window.title[:120],
            "body": window.body[:4000],
            "kind": window.kind,
            "size": window.size,
            "time": window.time_type,
            "place": window.placement,
            "ttl": "" if window.ttl_ms is None else str(window.ttl_ms),
            "lookout": "1" if window.lookout else "0",
            "items": "|".join(window.items)[:1500],
            "questions": "|".join(window.questions)[:1500],
            "recommendation": window.recommendation or "",
            "source": window.source or "",
            "response": window.response or "",
            "layout": window.layout or "",
            "dx": str(window.drift_x),
            "dy": str(window.drift_y),
            "tilt": f"{window.tilt:.2f}",
        },
        quote_via=quote,
    )
    return f"{_base_web()}/app/lookout?{query}"


def stage_web_url(plan: SurfacePlan) -> str:
    payload = json.dumps(plan.as_dict(), separators=(",", ":"), ensure_ascii=False)
    return f"{_base_web()}/app/stage#{quote(payload)}"


def dismiss_url(window_id: str | None = None) -> str:
    if window_id:
        return f"ev://dismiss?{urlencode({'id': window_id})}"
    return "ev://dismiss-all"


def _helper_path() -> Path | None:
    configured = getattr(settings, "notify_macos_helper_path", "") or ""
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return path
    bundled = Path("/Applications/EV.app/Contents/MacOS/EVNotificationHelper")
    return bundled if bundled.is_file() else None


def _in_pytest() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get("EV_PRESENCE_LIVE") != "1")


def _web_fallback_enabled() -> bool:
    flag = os.environ.get("EV_LOOKOUT_WEB_FALLBACK", "1").strip().lower()
    return flag not in {"0", "false", "no", "off"}


async def _run(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return await asyncio.to_thread(
        subprocess.run,
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


async def _open_native(window: SurfaceWindow) -> dict[str, Any]:
    url = present_url(
        title=window.title,
        body=window.body,
        kind=window.kind,
        size=window.size,
        time_type=window.time_type,
        placement=window.placement,
        ttl_ms=window.ttl_ms,
        items=window.items,
        recommendation=window.recommendation,
        source=window.source,
        window_id=window.id,
        lookout=window.lookout,
        lat=window.origin_lat,
        lon=window.origin_lon,
        dest_lat=window.dest_lat,
        dest_lon=window.dest_lon,
        questions=window.questions,
        response=window.response,
        layout=window.layout,
        drift_x=window.drift_x,
        drift_y=window.drift_y,
        tilt=window.tilt,
    )
    helper = _helper_path()
    helper_error = "EVNotificationHelper not installed"
    if helper is not None:
        command = [
            str(helper),
            "--present",
            "--title",
            window.title[:120],
            "--body",
            window.body[:4000],
            "--kind",
            window.kind,
            "--id",
            window.id,
            "--size",
            window.size,
            "--time-type",
            window.time_type,
            "--place",
            window.placement,
        ]
        if window.ttl_ms is not None:
            command.extend(["--ttl", str(window.ttl_ms)])
        if window.items:
            command.extend(["--items", "|".join(window.items)[:1500]])
        if window.questions:
            command.extend(["--questions", "|".join(window.questions)[:1500]])
        if window.recommendation:
            command.extend(["--recommendation", window.recommendation[:400]])
        if window.source:
            command.extend(["--source", window.source[:160]])
        if window.response:
            command.extend(["--response", window.response[:4000]])
        if window.layout:
            command.extend(["--layout", window.layout])
        command.extend(["--dx", str(window.drift_x), "--dy", str(window.drift_y), "--tilt", f"{window.tilt:.2f}"])
        if window.lookout:
            command.append("--lookout")
        result = await _run(command, 15)
        if result.returncode == 0:
            return {"opened": True, "url": url, "via": "helper", "id": window.id}
        helper_error = (result.stderr or result.stdout or "").strip()[:300]
    result = await _run(["open", url], 10)
    if result.returncode == 0:
        return {"opened": True, "url": url, "via": "open", "id": window.id}
    reason = (result.stderr or result.stdout or helper_error or "open failed").strip()[:300]
    return {"opened": False, "url": url, "via": None, "id": window.id, "reason": reason}


async def _open_web(windows: list[SurfaceWindow], plan: SurfacePlan) -> dict[str, Any]:
    if not windows:
        return {"opened": False, "via": None, "reason": "no windows"}
    target = stage_web_url(plan) if len(windows) > 1 else lookout_web_url(windows[0])
    result = await _run(["open", target], 10)
    if result.returncode == 0:
        return {"opened": True, "url": target, "via": "web", "surface": "lookout-web"}
    reason = (result.stderr or result.stdout or "open failed").strip()[:300]
    return {"opened": False, "url": target, "via": None, "reason": reason}


def _package_step() -> str:
    return (
        "package and launch the native EVIE app: "
        "`cd macos && swift build -c release && ./scripts/package.sh && "
        "open ./build/EV.app` then grant it as the handler for ev://"
    )


async def open_presence(
    *,
    title: str = "EVIE",
    body: str = "",
    kind: str = "card",
    size: str | None = None,
    time_type: str | None = None,
    placement: str | None = None,
    ttl_ms: int | None = None,
    items: Iterable[str] | None = None,
    recommendation: str | None = None,
    source: str | None = None,
    window_id: str | None = None,
    lookout: bool | None = None,
    message: str | None = None,
    auto: bool = False,
    windows: list[dict[str, Any]] | list[SurfaceWindow] | None = None,
    plan: SurfacePlan | None = None,
    lat: float | None = None,
    lon: float | None = None,
    dest_lat: float | None = None,
    dest_lon: float | None = None,
    questions: Iterable[str] | None = None,
    response: str | None = None,
    layout: str | None = None,
) -> dict:
    """Ask the native EVIE app to float one or more HUD windows."""

    resolved_plan = plan
    resolved: list[SurfaceWindow]
    if windows:
        resolved = [
            window if isinstance(window, SurfaceWindow) else window_from_args(window)
            for window in windows
        ]
        resolved_plan = resolved_plan or SurfacePlan(
            open=True,
            windows=resolved,
            rationale="caller supplied windows",
            explicit=True,
        )
    elif auto or (kind or "").lower() in {"auto", "decide"}:
        resolved_plan = plan_surfaces(
            message or title,
            explicit=True,
            kind=None if (kind or "").lower() in {"auto", "decide", ""} else kind,
            title=title,
            body=body,
            items=items,
            recommendation=recommendation,
            source=source,
            size=size,
            time_type=time_type,
            placement=placement,
            ttl_ms=ttl_ms,
            lookout=lookout,
            window_id=window_id,
            origin_lat=lat,
            origin_lon=lon,
            dest_lat=dest_lat,
            dest_lon=dest_lon,
            questions=questions,
            response=response,
            layout=layout,
        )
        resolved = list(resolved_plan.windows)
    else:
        resolved = [
            make_window(
                kind=kind,
                title=title,
                body=body,
                size=size,
                time_type=time_type,
                placement=placement,
                ttl_ms=ttl_ms,
                items=items,
                recommendation=recommendation,
                source=source,
                window_id=window_id,
                lookout=lookout,
                origin_lat=lat,
                origin_lon=lon,
                dest_lat=dest_lat,
                dest_lon=dest_lon,
                questions=questions,
                response=response,
                layout=layout,
            )
        ]
        resolved_plan = SurfacePlan(
            open=True,
            windows=resolved,
            rationale=f"direct present kind={resolved[0].kind}",
            explicit=True,
        )

    urls = [
        present_url(
            title=window.title,
            body=window.body,
            kind=window.kind,
            size=window.size,
            time_type=window.time_type,
            placement=window.placement,
            ttl_ms=window.ttl_ms,
            items=window.items,
            recommendation=window.recommendation,
            source=window.source,
            window_id=window.id,
            lookout=window.lookout,
            lat=window.origin_lat,
            lon=window.origin_lon,
            dest_lat=window.dest_lat,
            dest_lon=window.dest_lon,
            questions=window.questions,
            response=window.response,
            layout=window.layout,
            drift_x=window.drift_x,
            drift_y=window.drift_y,
            tilt=window.tilt,
        )
        for window in resolved
    ]
    primary_url = urls[0] if urls else present_url(title=title, body=body, kind=kind)

    if _in_pytest():
        for window in resolved:
            lookout_registry.upsert(window.as_dict(), opened=False, via="pytest")
        return {
            "ok": True,
            "opened": False,
            "degraded": True,
            "surface": "overlay",
            "url": primary_url,
            "windows": [window.as_dict() for window in resolved],
            "plan": resolved_plan.as_dict() if resolved_plan else None,
            "reason": "pytest (set EV_PRESENCE_LIVE=1 to open a real overlay)",
            "next_step": _package_step(),
        }

    opened_any = False
    via = None
    last_reason = None
    results: list[dict[str, Any]] = []
    for window in resolved:
        outcome = await _open_native(window)
        results.append(outcome)
        lookout_registry.upsert(window.as_dict(), opened=bool(outcome.get("opened")), via=outcome.get("via"))
        if outcome.get("opened"):
            opened_any = True
            via = outcome.get("via")
        else:
            last_reason = outcome.get("reason")

    surface = "overlay"
    if not opened_any and _web_fallback_enabled() and resolved:
        web = await _open_web(resolved, resolved_plan or SurfacePlan(open=True, windows=resolved))
        if web.get("opened"):
            opened_any = True
            via = "web"
            surface = "lookout-web"
            primary_url = str(web.get("url") or primary_url)
            for window in resolved:
                lookout_registry.upsert(window.as_dict(), opened=True, via="web")
        else:
            last_reason = web.get("reason") or last_reason

    if opened_any:
        return {
            "ok": True,
            "opened": True,
            "surface": surface,
            "url": primary_url,
            "via": via,
            "windows": [window.as_dict() for window in resolved],
            "plan": resolved_plan.as_dict() if resolved_plan else None,
        }
    return {
        "ok": False,
        "opened": False,
        "degraded": True,
        "surface": surface,
        "url": primary_url,
        "windows": [window.as_dict() for window in resolved],
        "plan": resolved_plan.as_dict() if resolved_plan else None,
        "reason": last_reason or "open failed",
        "next_step": _package_step(),
    }


async def dismiss_presence(window_id: str | None = None) -> dict[str, Any]:
    url = dismiss_url(window_id)
    dismissed = lookout_registry.dismiss(window_id)
    if _in_pytest():
        return {"ok": True, "dismissed": dismissed, "url": url, "opened": False, "degraded": True}
    result = await _run(["open", url], 10)
    return {
        "ok": result.returncode == 0,
        "dismissed": dismissed,
        "url": url,
        "opened": result.returncode == 0,
        "reason": None if result.returncode == 0 else (result.stderr or "open failed")[:300],
    }
