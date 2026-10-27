"""tmux panes (read-only inventory) across the default server and any `-L` sockets."""

from __future__ import annotations

import os
import re
from typing import Any

from ..runtime import Runtime

_FMT = "#{session_name}\x1f#{window_index}\x1f#{pane_id}\x1f#{pane_current_path}\x1f#{pane_current_command}\x1f#{pane_title}\x1f#{pane_pid}"


def sockets(rt: Runtime) -> list[str | None]:
    """None is the default server; others are -L socket names under /private/tmp/tmux-<uid>."""
    found: list[str | None] = [None]
    base = f"/private/tmp/tmux-{os.getuid()}"
    p = rt.run(["ls", base], timeout=5)
    for name in (p.out or "").split():
        if name != "default" and re.fullmatch(r"[\w.-]{1,64}", name):
            found.append(name)
    return found


def panes(rt: Runtime, *, include_sockets: bool = True) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for sock in (sockets(rt) if include_sockets else [None]):
        cmd = ["tmux"] + (["-L", sock] if sock else []) + ["list-panes", "-a", "-F", _FMT]
        p = rt.run(cmd, timeout=6)
        if not p.ok:
            continue
        for line in (p.out or "").splitlines():
            f = line.split("\x1f")
            if len(f) < 7:
                continue
            out.append({"app": "tmux", "socket": sock or "default", "session": f[0],
                        "window_id": f[1], "tab_id": None, "terminal_id": f[2], "cwd": f[3],
                        "foreground_cmd": f[4], "title": f[5], "pid": int(f[6]) if f[6].isdigit() else None,
                        "tab_index": None, "selected": None, "session_hint": None})
    return out
