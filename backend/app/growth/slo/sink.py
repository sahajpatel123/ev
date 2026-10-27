"""GE5 turn-telemetry sink: one JSON line per turn, counts/timings only.

Schema (v1): {turn_id, ts, modality, kind, ok, duration_ms,
first_response_ms (voice only, null until instrumented elsewhere),
tool_calls, spoke, bg (background goal turn), cost_usd or null}.

Privacy: no utterances, no args, no tool results — kinds and counts only.
The writer never raises (telemetry must not break turns) and appends with
an exclusive lock. Consumed by compute.py (SLOs) and dashboard.py.
"""

from __future__ import annotations

import fcntl
import json
import time
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path.home() / ".ev" / "growth" / "turns.jsonl"


def sink_path() -> Path:
    try:
        from app.config import settings
        raw = str(getattr(settings, "growth_telemetry_file", "") or "").strip()
        if raw:
            return Path(raw).expanduser()
    except Exception:
        pass
    return DEFAULT_PATH


def record_turn(*, turn_id: str, t0: float, modality: str, kind: str,
                ok: bool, spoken: str = "", tool_calls: int = 0,
                bg: bool = False, cost_usd: float | None = None,
                first_response_ms: float | None = None) -> None:
    """Append one turn record. Never raises, never stores content."""
    try:
        from app.cognitive import telemetry as T
        snap = T.snapshot()
        if first_response_ms is None:
            a = snap.get("last_transcript_to_brain_ms")
            b = snap.get("last_brain_to_speech_ms")
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                first_response_ms = float(a) + float(b)
    except Exception:
        pass
    entry = {"turn_id": str(turn_id)[:64], "ts": time.time(),
             "modality": str(modality or "")[:16], "kind": str(kind or "")[:40],
             "ok": bool(ok), "duration_ms": round((time.time() - t0) * 1000, 1),
             "first_response_ms": first_response_ms,
             "tool_calls": int(tool_calls), "spoke": bool((spoken or "").strip()),
             "bg": bool(bg), "cost_usd": cost_usd}
    try:
        p = sink_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.write(json.dumps(entry) + "\n")
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass


def read_turns(since_h: float | None = None) -> list[dict[str, Any]]:
    p = sink_path()
    if not p.is_file():
        return []
    cutoff = time.time() - since_h * 3600 if since_h else 0
    rows = []
    for line in p.read_text().splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and float(e.get("ts") or 0) >= cutoff:
            rows.append(e)
    return rows
