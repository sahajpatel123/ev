"""Statistics for every reported rate: n and a 95% Wilson interval (B8)."""

from __future__ import annotations

import math


def wilson(k: int, n: int, z: float = 1.96) -> dict[str, float | int]:
    if n <= 0:
        return {"k": k, "n": n, "p": 0.0, "lo": 0.0, "hi": 1.0}
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return {"k": k, "n": n, "p": round(p, 4), "lo": round(max(0.0, centre - half), 4), "hi": round(min(1.0, centre + half), 4)}


def fmt(w: dict[str, float | int]) -> str:
    return f"{w['k']}/{w['n']} = {100 * float(w['p']):.1f}% (95% CI {100 * float(w['lo']):.1f}-{100 * float(w['hi']):.1f}%)"


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    xs = sorted(values)
    i = min(len(xs) - 1, max(0, math.ceil(q * len(xs)) - 1))
    return xs[i]
