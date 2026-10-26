"""Machine state that decides whether UI work is possible at all: screen lock,
display sleep, idle time, output volume. No permissions needed (CoreGraphics
session/display queries); degrades to "unknown" rather than guessing.
"""

from __future__ import annotations

import ctypes
import re
import sys
from typing import Any

from .runtime import Runtime, default_runtime


def _ctypes_state() -> dict[str, Any]:
    if sys.platform != "darwin":
        return {"locked": None, "display_asleep": None, "idle_s": None, "on_console": None}
    cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    cg.CGMainDisplayID.restype = ctypes.c_uint32
    cg.CGDisplayIsAsleep.argtypes = [ctypes.c_uint32]
    cg.CGDisplayIsAsleep.restype = ctypes.c_int
    cg.CGSessionCopyCurrentDictionary.restype = ctypes.c_void_p
    cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    cf.CFDictionaryGetValue.restype = ctypes.c_void_p
    cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    cf.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
    cf.CFBooleanGetValue.restype = ctypes.c_ubyte
    cg.CGEventSourceSecondsSinceLastEventType.restype = ctypes.c_double
    cg.CGEventSourceSecondsSinceLastEventType.argtypes = [ctypes.c_int, ctypes.c_uint32]

    def flag(d: int, key: str) -> bool | None:
        v = cf.CFDictionaryGetValue(d, cf.CFStringCreateWithCString(None, key.encode(), 0x08000100))
        return None if not v else bool(cf.CFBooleanGetValue(v))

    d = cg.CGSessionCopyCurrentDictionary()
    locked = on_console = None
    if d:
        # the lock key is only present while locked
        locked = bool(flag(d, "CGSSessionScreenIsLocked"))
        on_console = flag(d, "kCGSSessionOnConsoleKey")
    asleep = bool(cg.CGDisplayIsAsleep(cg.CGMainDisplayID()))
    idle = float(cg.CGEventSourceSecondsSinceLastEventType(1, 0xFFFFFFFF))
    return {"locked": locked, "display_asleep": asleep, "idle_s": round(idle, 1),
            "on_console": on_console}


def read(rt: Runtime | None = None, *, volume: bool = False) -> dict[str, Any]:
    """{locked, display_asleep, idle_s, on_console, [volume, muted]}."""
    rt = rt or default_runtime()
    provider = getattr(rt, "system_state", None)
    try:
        state = provider() if callable(provider) else _ctypes_state()
    except Exception:
        state = {"locked": None, "display_asleep": None, "idle_s": None, "on_console": None}
    if volume:
        p = rt.osascript("set v to get volume settings\n"
                         "return (output volume of v as text) & \",\" & (output muted of v as text)", timeout=8)
        m = re.match(r"(\d+|missing value),\s*(true|false)", p.out or "")
        if p.ok and m and m.group(1).isdigit():
            state["volume"] = int(m.group(1))
            state["muted"] = m.group(2) == "true"
    return state


def blocker(state: dict[str, Any]) -> tuple[str, str] | None:
    """(error code, honest sentence) when the Mac cannot be operated right now."""
    if state.get("locked"):
        return "locked", "Your Mac is locked, so I can't do that until it's unlocked."
    if state.get("display_asleep"):
        return "display_asleep", "Your Mac's display is asleep, so I can't do that right now."
    return None
