"""macOS screen-awareness collector (derived text only -- never raw pixels).

Two probe paths share one derived payload shape:

* native helper (``EV_SCREEN_NATIVE=1``): a small compiled Swift helper uses
  NSWorkspace/CGWindow/AX to read the frontmost app, window title, browser
  URL, app category and idle time.  Window titles and URLs may need Screen
  Recording / Accessibility permission; without them the helper reports the
  permission state and the collector degrades to the text fallback.
* AppleScript fallback (System Events) when the native helper is not enabled.

OCR of the frontmost window is never implemented here.  When the user
explicitly consents (``EV_SCREEN_OCR=1``) AND configures a capture hook
(``EV_SCREEN_CAPTURE_CMD``), the captured bytes are handed to Agent 6's
vision provider; only the derived OCR text is emitted.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass

from clients.collectors import _native

Runner = Callable[[str], str]


@dataclass
class ScreenState:
    app: str | None = None
    document: str | None = None
    code_file: str | None = None
    meeting: str | None = None
    category: str | None = None
    url: str | None = None
    idle_seconds: float | None = None
    summary: str | None = None

    def to_payload(self) -> dict:
        payload: dict = {}
        if self.app:
            payload["app"] = self.app
        if self.document:
            payload["document"] = self.document
        if self.code_file:
            payload["code_file"] = self.code_file
        if self.meeting:
            payload["meeting"] = self.meeting
        if self.category:
            payload["category"] = self.category
        if self.url:
            payload["url"] = self.url
        if self.idle_seconds is not None:
            payload["idle_seconds"] = round(self.idle_seconds, 1)
        if self.summary:
            payload["summary"] = self.summary
        return payload


def _default_runner(command: str) -> str:
    import shlex
    import subprocess

    result = subprocess.run(
        shlex.split(command),
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    return result.stdout.strip()


def _run(runner: Runner | None, command: str) -> str:
    if runner is None:
        return _default_runner(command)
    return runner(command) or ""


def _infer_code_file(document: str | None) -> str | None:
    if not document:
        return None
    match = re.search(
        r"([A-Za-z0-9_\-]+\.(?:py|js|ts|swift|kt|java|go|rs|c|h|cpp|hpp|md|json|yaml|yml))\b",
        document,
    )
    return match.group(1) if match else None


def _infer_meeting(app: str, document: str | None) -> str | None:
    lowered = f"{app} {document or ''}".lower()
    if any(token in lowered for token in ("zoom", "meet", "teams", "slack call", "facetime")):
        return app
    return None


def _state_from_native_data(data: dict) -> ScreenState | None:
    app = str(data.get("app_name") or "").strip() or None
    document = (
        str(data.get("window_title") or data.get("document") or "").strip() or None
    )
    idle_raw = data.get("idle_seconds")
    idle: float | None = None
    if isinstance(idle_raw, (int, float)):
        idle = float(idle_raw)
    if not app and not document:
        return None
    category = str(data.get("category") or "").strip() or None
    url = str(data.get("url") or "").strip() or None
    return ScreenState(
        app=app,
        document=document,
        code_file=_infer_code_file(document),
        meeting=_infer_meeting(app or "", document),
        category=category,
        url=url,
        idle_seconds=idle,
    )


def _native_screen_state() -> ScreenState | None:
    if sys.platform != "darwin":
        return None
    data = _native.run_helper(["--screen"], timeout=6)
    if not data:
        return None
    return _state_from_native_data(data)


def _apple_script_state(runner: Runner | None) -> ScreenState:
    try:
        app = _run(
            runner,
            'osascript -e \'tell application "System Events" to get name of first application process whose frontmost is true\'',
        )
    except Exception:  # noqa: BLE001 - collector boundary; degrade gracefully
        return ScreenState()
    app = app.strip()
    if not app:
        return ScreenState()
    document = None
    try:
        document = _run(
            runner,
            'osascript -e \'tell application "System Events" to get name of first window of first application process whose frontmost is true\'',
        ).strip()
    except Exception:  # noqa: BLE001 - window title is optional
        document = None
    document = document or None
    return ScreenState(
        app=app,
        document=document,
        code_file=_infer_code_file(document),
        meeting=_infer_meeting(app, document),
    )


def _ocr_enabled() -> bool:
    return os.environ.get("EV_SCREEN_OCR", "").lower() in {"1", "true", "yes"}


def _run_provider_ocr(data: bytes) -> str | None:
    """Run Agent 6's vision provider in a worker thread (no event-loop clash)."""

    import asyncio

    from app.vision.providers import get_vision_provider

    async def analyze() -> str | None:
        outcome = await get_vision_provider().analyze(
            data=data,
            content_type="image/png",
            filename="front-window.png",
        )
        text = (outcome.ocr_text or "").strip()
        return text[:1000] or None

    try:
        return asyncio.run(analyze())
    except Exception:  # noqa: BLE001 - OCR is optional; degrade to no summary
        return None


def _ocr_summary() -> str | None:
    """Explicitly-consented OCR of the front window via Agent 6's helper."""

    if not _ocr_enabled():
        return None
    command = os.environ.get("EV_SCREEN_CAPTURE_CMD")
    if not command:
        return None
    captured = _native.run_capture_command(command)
    if not captured:
        return None
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_run_provider_ocr, captured)
        try:
            return future.result(timeout=25)
        except Exception:  # noqa: BLE001
            return None


def screen_state(runner: Runner | None = None) -> ScreenState:
    """Derive active app + window context, native-first when opted in.

    Returns an empty :class:`ScreenState` when the OS query fails (e.g., no
    accessibility permission), so the agent degrades gracefully instead of
    capturing anything raw.
    """

    state: ScreenState | None = None
    if runner is not None:
        # Test seam: a runner may answer the native JSON protocol directly.
        try:
            native_json = _run(runner, "ambient screen json")
        except Exception:  # noqa: BLE001 - runner failures degrade to AppleScript
            native_json = ""
        if native_json.strip():
            try:
                state = _state_from_native_data(json.loads(native_json))
            except ValueError:
                state = None
        if state is None:
            state = _apple_script_state(runner)
    elif os.environ.get("EV_SCREEN_NATIVE", "").lower() in {"1", "true", "yes"}:
        state = _native_screen_state()
        if state is None:
            state = _apple_script_state(None)
    else:
        state = _apple_script_state(None)

    summary = _ocr_summary()
    if summary and state is not None:
        state.summary = summary
    return state or ScreenState()
