"""CUG runner. `python -m hands_cug.runner sim [id-prefix ...]` runs the offline tasks."""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.fabric.skills.packs import load_all

from . import tasks_corpus, tasks_live, tasks_live_term, tasks_sim  # noqa: F401  (registers tasks)
from .core import TASKS, SimEnv, routing, run_task
from .stats import fmt, wilson


def run(modes: set[str], prefixes: list[str]) -> list[Any]:
    load_all()
    out = []
    for t in TASKS:
        if t.mode not in modes or (prefixes and not any(t.id.startswith(p) for p in prefixes)):
            continue
        env = SimEnv()
        out.append(run_task(t, env))
    return out


def run_live(prefixes: list[str], *, term: bool = False) -> list[Any]:
    from .live import LiveEnv

    load_all()
    out = []
    mode = "live_term" if term else "live"
    chosen = [t for t in TASKS if t.mode == mode and (not prefixes or any(t.id.startswith(p) for p in prefixes))]
    with LiveEnv(chrome=not term, ghostty=term, purpose="CUG live terminal run" if term else "CUG live run") as env:
        for t in chosen:
            if env.chrome:
                env.close_tabs()
            sc = run_task(t, env)
            try:
                sc.scope_violation = env.scope_violation()
            except Exception:
                sc.scope_violation = False
            if term:
                for wid in list(env.created_ghostty):  # no session may stay running into the next task
                    try:
                        env.ghostty.close_window(wid)
                    except Exception:
                        pass
                env.created_ghostty.clear()
                time.sleep(0.8)
            print(f"{'PASS' if sc.passed else 'FAIL'} {t.id:46s} {sc.seconds:5.1f}s {sc.note[:110]}", flush=True)
            out.append(sc)
        if env.chrome:
            env.close_tabs()
    return out


def summarize(scored: list[Any]) -> dict[str, Any]:
    by_dom: dict[str, list[Any]] = defaultdict(list)
    for s in scored:
        by_dom[s.domain].append(s)
    dom = {d: wilson(sum(x.passed for x in xs), len(xs)) for d, xs in sorted(by_dom.items())}
    return {"n": len(scored), "passed": sum(s.passed for s in scored), "overall": wilson(sum(s.passed for s in scored), len(scored)),
            "false_success": sum(s.false_success for s in scored), "crashed": sum(s.crashed for s in scored),
            "domains": dom}


def save(mode: str, scored: list[Any]) -> Path:
    import dataclasses

    out = Path(__file__).resolve().parents[2] / "docs" / "cug"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{mode}.json"
    path.write_text(json.dumps([dataclasses.asdict(s) for s in scored], indent=1, default=str))
    return path


if __name__ == "__main__":
    modes = {sys.argv[1]} if len(sys.argv) > 1 else {"sim", "fs"}
    if "sim" in modes:
        modes.add("fs")
    res = run_live(sys.argv[2:], term=modes == {"live_term"}) if modes <= {"live", "live_term"} else run(modes, sys.argv[2:])
    for s in res:
        if not s.passed:
            print(f"FAIL {s.id:44s} {'FALSE-SUCCESS ' if s.false_success else ''}{s.note[:160]} | say={s.say!r} err={s.error_code}")
    if not sys.argv[2:]:
        save(sys.argv[1] if len(sys.argv) > 1 else "sim", res)
    sm = summarize(res)
    print("scope violations:", sum(getattr(x, "scope_violation", False) for x in res))
    print(json.dumps({k: (v if k != "domains" else {d: fmt(w) for d, w in v.items()}) for k, v in sm.items() if k != "overall"}, indent=1))
    print("overall", fmt(sm["overall"]))
    r = routing([t for t in TASKS if t.mode in modes])
    print(f"routing top-1 {r['top1']}/{r['n']}  top-3 {r['top3']}/{r['n']}")
    for m in r["misses"][:12]:
        print("  miss", m)
