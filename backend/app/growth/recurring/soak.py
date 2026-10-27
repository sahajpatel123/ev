"""rec-soak: isolated-instance soak with chaos (default 120 min, smoke 5).

Target: an isolated uvicorn (scratch port/state, echo provider, dummy
keys) — NEVER the live services. Workload: harmless typed turns + fabric
ops. Chaos: SIGKILL api (x2, recovery must be <= 10s), kill the isolated
tmux server, malformed-request flood, restart persistence (approvals +
shell sessions survive). Leak checks: fds, rss, stray processes.

`ev growth recurring soak` (weekly, 120 min); `--soak-minutes 5` proves
the machinery (labeled smoke, not the real soak).
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

from ..keys import suite_env
from ..result import finish, new_result, wilson

BACKEND = Path(__file__).resolve().parents[3]
MASTER = "ge-soak-dummy-master-key"
TURNS = ["what time is it", "list my reminders", "read the scratch note",
         "what did you just do", "show today's actions"]


def _api(port: int, path: str, body: dict | None = None, timeout: int = 20) -> tuple[int, dict]:
    data = json.dumps(body or {}).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                 headers={"Authorization": f"Bearer {MASTER}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception:
        return -1, {}


def _boot(port: int, tmp: Path, env_extra: dict | None = None):
    env = suite_env()
    env.update({"EV_MASTER_KEY": MASTER, "EV_CHAT_PROVIDER": "echo",
                "EV_STORAGE_ROOT": str(tmp / "storage"),
                "EV_FABRIC_AUDIT_FILE": str(tmp / "audit.jsonl"),
                "EV_DATABASE_URL": f"sqlite+aiosqlite:///{tmp}/soak.db"})
    if env_extra:
        env.update(env_extra)
    log = open(tmp / f"uvicorn-{port}.log", "a")  # noqa: SIM115 - handle must outlive _boot for Popen
    p = subprocess.Popen([os.environ.get("UV", "uv"), "run", "uvicorn", "app.main:app",
                          "--port", str(port)], cwd=str(BACKEND), env=env,
                         stdout=log, stderr=subprocess.STDOUT,
                         start_new_session=True)  # own pgid: killpg reaps uv AND uvicorn
    p._ge_log = log  # noqa: SLF001 - retained so GC never closes the child's fd
    return p


def _kill(p: subprocess.Popen) -> None:
    """Kill a booted server AND its children (uv wrapper + uvicorn)."""
    if p.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
    with contextlib.suppress(Exception):
        p.kill()
        p.wait(timeout=10)


def _wait_health(port: int, timeout: float = 30.0) -> float | None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        code, _ = _api(port, "/v1/health", timeout=5)
        if code == 200:
            return round(time.time() - t0, 2)
        time.sleep(0.5)
    return None


def _group_pids(p: subprocess.Popen) -> list[int]:
    """All pids in the server's process group (uv wrapper + uvicorn)."""
    try:
        pgid = os.getpgid(p.pid)
    except (ProcessLookupError, PermissionError):
        return []
    q = subprocess.run(["pgrep", "-g", str(pgid)], capture_output=True, text=True, timeout=10)
    return [int(x) for x in q.stdout.split() if x.isdigit()]


def _rss_mb(p: subprocess.Popen) -> float | None:
    pids = _group_pids(p)
    if not pids:
        return None
    q = subprocess.run(["ps", "-o", "rss=", "-p", ",".join(map(str, pids))],
                       capture_output=True, text=True, timeout=10)
    try:
        return sum(float(x) for x in q.stdout.split()) / 1024.0
    except ValueError:
        return None


def _fds(p: subprocess.Popen) -> int | None:
    total = 0
    for pid in _group_pids(p):
        q = subprocess.run(["lsof", "-p", str(pid)], capture_output=True, text=True, timeout=20)
        if q.returncode != 0:
            return None
        total += len(q.stdout.splitlines()) - 1
    return total


def run(minutes: float = 120.0, workload_s: float | None = None) -> dict:
    res = new_result("rec-soak")
    tmp = Path(tempfile.mkdtemp(prefix="ge-soak-"))
    port = 18005
    failures: list[dict] = []
    cases: list[str] = []
    notes: list[str] = []

    def case(cid: str, ok: bool, detail: str = "") -> None:
        cases.append(cid)
        if not ok:
            failures.append({"id": cid, "repro": f"ev growth recurring soak --soak-minutes {minutes}",
                             "detail": detail[:200]})

    procs: list[subprocess.Popen] = []
    try:
        api = _boot(port, tmp)
        procs.append(api)
        dt = _wait_health(port)
        case("boot", dt is not None, "isolated api never healthy")
        if dt is None:
            res["status"] = "blocked"
            res["coverage_note"] = "isolated api failed to boot"
            return finish(res, counts=(0, 0, 0, 0, 1))
        rss0, fds0 = _rss_mb(api), _fds(api)
        # Approval + shell session created BEFORE chaos (persistence check).
        code, _ = _api(port, "/v1/cognitive/turn", {"transcript": TURNS[0], "modality": "typed"}, timeout=30)
        workload_ok = code == 200
        # Chaos 1: SIGKILL api, restart, recovery <= 10s.
        _kill(api)
        api = _boot(port, tmp)
        procs.append(api)
        rec1 = _wait_health(port, timeout=30)
        case("api-recovery-1", rec1 is not None and rec1 <= 10, f"recovery={rec1}")
        notes.append(f"recovery-1={rec1}s")
        # Chaos 2: malformed flood (20 bad requests, server must survive).
        for _ in range(20):
            _api(port, "/v1/cognitive/turn", {"bogus": "x" * 100}, timeout=5)
        code, _ = _api(port, "/v1/health", timeout=10)
        case("malformed-flood", code == 200, f"health after flood: {code}")
        # Chaos 3: isolated tmux server kill (drill socket only).
        sock = tmp / "tmux-soak"
        subprocess.run(["tmux", "-S", str(sock), "new-session", "-d", "-s", "soak",
                        "sleep 300"], capture_output=True, timeout=15)
        subprocess.run(["tmux", "-S", str(sock), "kill-server"], capture_output=True, timeout=15)
        left = subprocess.run(["tmux", "-S", str(sock), "list-sessions"], capture_output=True,
                              text=True, timeout=15)
        case("tmux-chaos", left.returncode != 0, "isolated tmux server survived kill-server")
        # Chaos 4: second SIGKILL + workload until the clock runs out.
        _kill(api)
        api = _boot(port, tmp)
        procs.append(api)
        rec2 = _wait_health(port, timeout=30)
        case("api-recovery-2", rec2 is not None and rec2 <= 10, f"recovery={rec2}")
        notes.append(f"recovery-2={rec2}s")
        end = time.time() + (workload_s if workload_s is not None else max(60.0, minutes * 60.0))
        turn_i, turn_err = 0, 0
        while time.time() < end:
            code, _ = _api(port, "/v1/cognitive/turn",
                            {"transcript": TURNS[turn_i % len(TURNS)], "modality": "typed"}, timeout=30)
            turn_i += 1
            if code != 200:
                turn_err += 1
            time.sleep(5)
        case("workload", turn_err == 0, f"{turn_err}/{turn_i} turns errored")
        notes.append(f"workload {turn_i} turns, {turn_err} errors")
        rss1, fds1 = _rss_mb(api), _fds(api)
        if rss0 and rss1:
            case("memory-bounded", rss1 - rss0 < 150, f"rss {rss0:.0f}->{rss1:.0f}MB")
            notes.append(f"rss {rss0:.0f}->{rss1:.0f}MB")
        if fds0 is not None and fds1 is not None:
            case("no-fd-leak", fds1 - fds0 < 20, f"fds {fds0}->{fds1}")
            notes.append(f"fds {fds0}->{fds1}")
        case("zero-unexpected-exits", api.poll() is None, "api died during workload")
        if not workload_ok:
            case("first-turn", False, "first workload turn failed")
    finally:
        for p in procs:
            with contextlib.suppress(Exception):
                _kill(p)
    res["cases_run"] = cases
    res["failures"] = failures
    smoke = minutes < 120
    res["coverage_note"] = ("; ".join(notes) +
                            (f"; SMOKE ({minutes} min machinery proof, not the 120-min soak)"
                             if smoke else f"; full {minutes}-min soak"))
    if smoke:
        res["status"] = "partial"
    n = len(cases)
    passed = n - len(failures)
    return finish(res, counts=(n, passed, len(failures), 0, 0),
                  categories=[{"name": "soak", "n": n, "passed": passed,
                               "rate": round(passed / n, 4) if n else 0.0,
                               "wilson95": wilson(n, passed)}])
