"""Home Station Chrome tab JS — no window activation, no cookie export.

Used by WhatsApp Web and generic browser operations. Selectors stay here.
Muse never sees this module's output keys beyond semantic facts.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from typing import Any

_CHROME_NAMES = ("Google Chrome", "Chromium", "Chrome")


async def chrome_running() -> bool:
    if not shutil.which("pgrep"):
        return False
    proc = await asyncio.create_subprocess_exec(
        "pgrep", "-x", "Google Chrome",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    code = await proc.wait()
    return code == 0


async def eval_in_tab(*, url_contains: str, javascript: str, timeout: float = 20.0) -> dict[str, Any]:
    """Execute JS in an existing tab. Never activates Chrome. Never launches it."""
    if not await chrome_running():
        return {
            "ok": False,
            "error": "chrome_not_running",
            "authenticated": False,
            "activated": False,
            "focus_theft": 0,
        }
    if not shutil.which("osascript"):
        return {
            "ok": False,
            "error": "osascript_unavailable",
            "authenticated": False,
            "activated": False,
            "focus_theft": 0,
        }
    script = """
on run argv
  set needle to item 1 of argv
  set js to item 2 of argv
  tell application "System Events"
    if not (exists process "Google Chrome") then
      return "{\\"ok\\":false,\\"error\\":\\"chrome_not_running\\",\\"activated\\":false}"
    end if
  end tell
  tell application "Google Chrome"
    repeat with w in windows
      repeat with t in tabs of w
        set tabUrl to ""
        try
          set tabUrl to URL of t
        end try
        if tabUrl contains needle then
          try
            set raw to execute t javascript js
            return raw as string
          on error errMsg number errNum
            if errNum is 12 then
              return "{\\"ok\\":false,\\"error\\":\\"javascript_apple_events_disabled\\",\\"authenticated\\":false,\\"activated\\":false,\\"focus_theft\\":0,\\"diagnosis\\":\\"javascript_apple_events_disabled\\"}"
            end if
            return "{\\"ok\\":false,\\"error\\":\\"js_execute_failed\\",\\"authenticated\\":false,\\"activated\\":false,\\"focus_theft\\":0,\\"diagnosis\\":\\"js_execute_failed\\"}"
          end try
        end if
      end repeat
    end repeat
  end tell
  return "{\\"ok\\":false,\\"error\\":\\"tab_not_found\\",\\"authenticated\\":false,\\"activated\\":false}"
end run
"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript",
            "-",
            url_contains,
            javascript,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(script.encode("utf-8")),
            timeout=timeout,
        )
    except TimeoutError:
        return {"ok": False, "error": "js_timeout", "activated": False, "focus_theft": 0}
    except Exception as exc:
        return {
            "ok": False,
            "error": type(exc).__name__,
            "activated": False,
            "focus_theft": 0,
        }
    if proc.returncode != 0:
        err = (stderr or b"").decode("utf-8", errors="replace")[:200]
        return {
            "ok": False,
            "error": "osascript_failed",
            "diagnosis": err or "osascript_failed",
            "activated": False,
            "focus_theft": 0,
        }
    raw = (stdout or b"").decode("utf-8", errors="replace").strip()
    return _parse_js_json(raw)


def _parse_js_json(raw: str) -> dict[str, Any]:
    if not raw:
        return {"ok": False, "error": "empty_js_result", "activated": False, "focus_theft": 0}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            data.setdefault("activated", False)
            data.setdefault("focus_theft", 0)
            return data
    except json.JSONDecodeError:
        pass
    return {
        "ok": False,
        "error": "ui_changed",
        "diagnosis": "non_json_js_result",
        "activated": False,
        "focus_theft": 0,
        "raw_present": True,
    }


def wrap_js(body: str) -> str:
    """IIFE that always returns JSON text. No cookies/localStorage dumps."""
    return (
        "(function(){try{"
        f"{body}"
        "}catch(e){return JSON.stringify({ok:false,error:String(e&&e.message||e),diagnosis:'js_exception'});}})()"
    )
