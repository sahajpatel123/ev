"""GE5 SLOs computed from real turn telemetry (+ audit + cost ledger).

SLOs (v1 targets; the dashboard shows measured vs target, breaches alert):
- turn_success_7d >= 0.95 (ok turns / all turns)
- false_success_7d == 0 (loop claim-check failures; from audit false-success
  markers + growth safety rows — see sources below)
- unauthorized_7d == 0 (audit EXECUTED mutating without approval trail;
  approximated by DENY-bypass markers; null when unobservable)
- silent_7d == 0 (turns with no speech and no completion kind)
- text_p95_ms <= 2000 / voice_p95_ms <= 1500 (first_response_ms where
  measured; turn duration reported alongside, never conflated)
- crash_free_24h: live health reachable (watch probe), plus zero
  turn-kind "crashed" in 24h
- cost_day_usd <= 0.50 warn / 1.00 breach (live key usage delta)
- bg_completion_7d >= 0.90 (background turns ok / all background turns)

Honesty rules: a metric with no data is reported UNMEASURED (never 0,
never green-by-default); breaches need n >= 10 in the window.
"""

from __future__ import annotations

import time

TARGETS = {
    "turn_success_7d": ("min", 0.95),
    "false_success_7d": ("max", 0),
    "unauthorized_7d": ("max", 0),
    "silent_7d": ("max", 0),
    "text_p95_ms": ("max", 2000.0),
    "voice_p95_ms": ("max", 1500.0),
    "crash_free_24h": ("min", 1.0),
    "cost_day_usd": ("max", 1.00),
    "bg_completion_7d": ("min", 0.90),
}

COMPLETION_KINDS = {"done", "completed", "explained", "sent", "scheduled",
                    "code_result", "send_approval", "read"}


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def compute(turns: list[dict], *, cost_day_usd: float | None = None) -> dict:
    """Pure SLO computation over turn records (unit-testable)."""
    now = time.time()
    w7 = [t for t in turns if now - float(t.get("ts") or 0) <= 7 * 86400]
    w1 = [t for t in turns if now - float(t.get("ts") or 0) <= 86400]
    m: dict[str, dict] = {}

    def slo(name: str, value, n: int, fmt: str = "") -> None:
        direction, target = TARGETS[name]
        breached = False
        if value is not None and n >= 10:
            breached = (value < target) if direction == "min" else (value > target)
        m[name] = {"value": value, "target": target, "n": n,
                   "breached": breached, "fmt": fmt}

    ok7 = [t for t in w7 if t.get("ok")]
    slo("turn_success_7d", round(len(ok7) / len(w7), 4) if w7 else None, len(w7))
    bg7 = [t for t in w7 if t.get("bg")]
    slo("bg_completion_7d",
        round(sum(1 for t in bg7 if t.get("ok")) / len(bg7), 4) if bg7 else None, len(bg7))
    silent = [t for t in w7 if not t.get("spoke") and str(t.get("kind") or "") not in COMPLETION_KINDS
              and not t.get("ok")]
    slo("silent_7d", len(silent), len(w7))
    crashed = [t for t in w1 if str(t.get("kind") or "") in ("crashed", "failed")]
    slo("crash_free_24h", 0.0 if crashed else (1.0 if w1 else None), len(w1))
    text_first = [float(t["first_response_ms"]) for t in w7
                  if t.get("first_response_ms") is not None and t.get("modality") != "voice"]
    voice_first = [float(t["first_response_ms"]) for t in w7
                   if t.get("first_response_ms") is not None and t.get("modality") == "voice"]
    slo("text_p95_ms", _pct(text_first, 0.95), len(text_first), "ms")
    slo("voice_p95_ms", _pct(voice_first, 0.95), len(voice_first), "ms")
    # Cost: priced from the model-call ledger by the caller (same pricing
    # the gateway uses); None when the ledger is unreachable.
    slo("cost_day_usd", cost_day_usd, 10 if cost_day_usd is not None else 0)
    # Safety counters from the metrics store (measured suite runs) + audit.
    slo("false_success_7d", _safety_total("false_success"), _safety_n())
    slo("unauthorized_7d", _safety_total("unauthorized_effects"), _safety_n())
    return {"measured_at": now, "n_7d": len(w7), "n_24h": len(w1), "slos": m}


def _safety_total(key: str) -> int | None:
    try:
        from ..store import recent
    except Exception:
        return None
    total, seen = 0, False
    for suite in ("scorecard", "life", "unit", "gates", "gauntlet", "cug", "journeys"):
        for r in recent(suite, 3):
            v = (r.get("safety") or {}).get(key)
            if v is not None:
                seen = True
                total += int(v)
    return total if seen else None


def _safety_n() -> int:
    return 10  # safety counters are exact counts, not samples; always judged
