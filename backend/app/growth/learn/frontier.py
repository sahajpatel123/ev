"""GE4 CAPABILITY FRONTIER: what the owner asked that Evie could not do.

Ranked by (frequency, recency) over mined capability gaps; each row names
the closest existing capability by token overlap against the LIVE tool
inventory (robust to renames). Deterministic, no LLM.
"""

from __future__ import annotations

import re


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9']+", s.lower())) - {
        "a", "the", "to", "it", "please", "my", "me", "i", "can", "you"}


def inventory() -> list[tuple[str, str]]:
    try:
        from app.cognitive.capabilities import tool_specs
        return [(t.name, f"{t.name} {t.description}") for t in tool_specs()]
    except Exception:
        return []


def closest(utterance: str, inv: list[tuple[str, str]] | None = None) -> str:
    inv = inventory() if inv is None else inv
    ut = _tokens(utterance)
    best, score = "none", 0.0
    for name, text in inv:
        tt = _tokens(text)
        if not ut or not tt:
            continue
        s = len(ut & tt) / len(ut | tt)
        if s > score:
            best, score = name, s
    return best if score > 0 else "none"


def _overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def frontier(gaps: list[dict], top: int = 10) -> list[dict]:
    inv = inventory()
    groups: list[list[dict]] = []
    for g in gaps:
        gt = _tokens(g.get("utterance", ""))
        placed = False
        for grp in groups:
            if _overlap(gt, _tokens(grp[0].get("utterance", ""))) >= 0.5:
                grp.append(g)
                placed = True
                break
        if not placed:
            groups.append([g])
    rows = []
    for members in groups:
        ats = sorted(m.get("at", "") for m in members)
        rows.append({"utterance": members[-1].get("utterance", "")[:200],
                     "count": len(members), "last_at": ats[-1] if ats else "",
                     "closest_capability": closest(members[-1].get("utterance", ""), inv)})
    rows.sort(key=lambda r: (r["count"], r["last_at"]), reverse=True)
    return rows[:top]
