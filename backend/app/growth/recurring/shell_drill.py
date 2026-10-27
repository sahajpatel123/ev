"""rec-shell-drill: live drill of shell sessions (spawn/reattach/cancel, $0).

Uses a scratch registry root; process cleanup is guaranteed by finally
(tmux name ev-<sid> is unique per run, killed even on crash). Takes the
GUI lock (default tmux server is shared state).
"""

from __future__ import annotations

import subprocess
import tempfile
import time
from pathlib import Path

from ..result import finish, new_result, wilson


def run() -> dict:
    from app.fabric.shell_sessions import ShellSessions
    from app.fabric.skills.guilock import gui_lock

    res = new_result("rec-shell-drill")
    tmp = Path(tempfile.mkdtemp(prefix="ge-shell-drill-"))
    S = ShellSessions(root=tmp)
    cases: list[str] = []
    failures: list[dict] = []

    def case(cid: str, ok: bool, detail: str = "") -> None:
        cases.append(cid)
        if not ok:
            failures.append({"id": cid, "repro": "ev growth recurring shell-drill",
                             "detail": detail[:200]})

    sid = ""
    try:
        with gui_lock(owner="rocky-sculptor", purpose="rec-shell-drill"):
            r = S.run("echo drill-ok", timeout=15.0)
            out = (r.get("evidence") or {}).get("out", "")
            case("oneshot", bool(r.get("ok")) and "drill-ok" in out, str(r)[:200])
            s = S.start("echo line1 && sleep 120", cwd=str(tmp))
            sid = s.get("session_id") or ""
            case("spawn", bool(s.get("ok")) and bool(sid), str(s)[:200])
            if sid:
                t: dict = {}
                for _ in range(10):  # tmux capture lags the write; poll like T2
                    t = S.stream(sid)  # full pane: tail(n) would drop line 1
                    if "line1" in str(t.get("output") or ""):
                        break
                    time.sleep(1.0)
                case("reattach-tail", bool(t.get("ok")) and "line1" in str(t.get("output") or ""),
                     str(t)[:200])
                c = S.cancel(sid)
                case("cancel", bool(c.get("ok")), str(c)[:200])
                S2 = ShellSessions(root=tmp)  # fresh handle, same persisted registry
                ra = S2.reattach(sid)
                ec = S2.exit_code(sid)
                case("cancelled-stays-dead",
                     ra.get("ok") is False and ec.get("exit_code") == -1,
                     f"reattach={ra} exit={ec}"[:200])
    finally:
        if sid:
            with_gui = True
            try:
                with gui_lock(owner="rocky-sculptor", purpose="rec-shell-drill-cleanup"):
                    S.cancel(sid)
            except Exception:
                with_gui = False
            subprocess.run(["tmux", "kill-session", "-t", f"ev-{sid}"],
                           capture_output=True, timeout=15)
            if not with_gui:
                res["coverage_note"] = "cleanup ran without GUI lock (lock busy); unique tmux name killed directly"
    if "coverage_note" not in res:
        res["coverage_note"] = "scratch registry root; default tmux server under GUI lock; unique ev-<sid> names"
    res["cases_run"] = cases
    res["failures"] = failures
    n = len(cases)
    passed = n - len(failures)
    return finish(res, counts=(n, passed, len(failures), 0, 0),
                  categories=[{"name": "shell", "n": n, "passed": passed,
                               "rate": round(passed / n, 4) if n else 0.0,
                               "wilson95": wilson(n, passed)}])
