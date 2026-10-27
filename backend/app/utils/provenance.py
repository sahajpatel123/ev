"""Provenance stamping for mined records (GE4 source filter).

Every failure signal the miners ingest must be attributable to REAL
owner activity. Writers stamp each record with `src`:

- "owner": live services, owner actor, no eval markers. The ONLY value
  miners ingest. (Owner channels: CLI, Talk, PWA, gateway, Shortcuts,
  routines, voice — all arrive without eval markers.)
- "suite": EV_GROWTH_EVAL=1 (eval suites, RC/gate children, shadow
  replays, gauntlet/CUG lanes, bake-offs). Excluded.
- "pytest": PYTEST_CURRENT_TEST set (test harness). Excluded.
- "live-proof": inside a captain-declared drill window
  (~/.ev/drill_window.json). Excluded. `ev accept` and the train's
  post-deploy checks manage the window automatically; hand-run live
  drills must write one first (see write_drill_window).
- missing/anything else: unprovenanced (all pre-stamp history).
  Excluded — fail-closed. Historical rows can never be promoted.

This is provenance by construction (ambient markers + explicit
declarations recorded at write time), never content pattern matching.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

OWNER_SRC = "owner"
SUITE_SRC = "suite"
PYTEST_SRC = "pytest"
DRILL_SRC = "live-proof"


def drill_window_path() -> Path:
    return Path.home() / ".ev" / "drill_window.json"


def in_drill_window(now: float | None = None) -> bool:
    """True when a live-drill window is currently open (any reason)."""
    try:
        raw = json.loads(drill_window_path().read_text())
        lo, hi = float(raw.get("start", 0)), float(raw.get("end", 0))
    except (OSError, ValueError, TypeError):
        return False
    t = time.time() if now is None else now
    return lo <= t <= hi


def write_drill_window(seconds: float, reason: str) -> Path:
    """Open a drill window (captain tooling only). Returns the path."""
    p = drill_window_path()
    t = time.time()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"start": t, "end": t + seconds, "reason": reason}))
    tmp.rename(p)
    return p


def clear_drill_window() -> None:
    import contextlib

    with contextlib.suppress(OSError):
        drill_window_path().unlink()


def stamp(explicit: str | None = None) -> str:
    """Provenance for a record being written right now."""
    if explicit:
        return explicit
    if os.environ.get("EV_GROWTH_EVAL") == "1":
        return SUITE_SRC
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return PYTEST_SRC
    if in_drill_window():
        return DRILL_SRC
    return OWNER_SRC


def is_owner(src: str | None) -> bool:
    """Miner gate: only explicitly owner-stamped records are ingested."""
    return src == OWNER_SRC
