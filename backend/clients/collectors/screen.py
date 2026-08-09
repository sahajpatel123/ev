"""macOS screen-awareness collector (derived text only — never raw pixels)."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

Runner = Callable[[str], str]


@dataclass
class ScreenState:
    app: str | None = None
    document: str | None = None
    code_file: str | None = None
    meeting: str | None = None

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
    match = re.search(r"([A-Za-z0-9_\-]+\.(?:py|js|ts|swift|kt|java|go|rs|c|h|cpp|hpp|md|json|yaml|yml))\b", document)
    return match.group(1) if match else None


def screen_state(runner: Runner | None = None) -> ScreenState:
    """Derive active app + window title via System Events (accessibility-safe).

    Returns an empty :class:`ScreenState` when the OS query fails (e.g., no
    accessibility permission), so the agent degrades gracefully instead of
    capturing anything raw.
    """
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
    lowered = f"{app} {document or ''}".lower()
    meeting = None
    if any(token in lowered for token in ("zoom", "meet", "teams", "slack call", "facetime")):
        meeting = app
    return ScreenState(
        app=app,
        document=document,
        code_file=_infer_code_file(document),
        meeting=meeting,
    )
