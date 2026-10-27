"""GE4 miners: deterministic failure-signal extraction from durable records.

Sources mined (no LLM, no guessing):
- audit log: failed/unverified receipts (ok=false), undo-within-10-min
  (an undo op whose target matches an action in the prior 10 min),
  denied approvals (decision DENY).
- turn ledger: owner corrections ("no", "that's wrong", ...), rephrased
  repeats within 2 min (token-overlap >= 0.6 on consecutive owner turns),
  cancelled/abandoned/timeout kinds.
- explicit: `ev bad` / HUD thumbs-down land directly in the queue (no miner).

All miners are pure over injected paths/rows (unit-testable with fixtures).
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

CORRECTION_RES = [
    re.compile(r"^\s*(no+[\s!.]*)+$", re.I),
    re.compile(r"that'?s (wrong|not (it|right|what i (meant|asked)))", re.I),
    re.compile(r"\bnot that one\b", re.I),
    re.compile(r"\bi (meant|said)\b", re.I),
    re.compile(r"\bwrong (one|answer|thing)\b", re.I),
    re.compile(r"^stop\.?$", re.I),
]
CANCEL_KINDS = {"cancelled", "abandoned", "timeout", "phone_cancel", "crashed"}
FAIL_KINDS = {"failed", "crashed", "timeout", "unavailable"}
CANT_RES = [
    re.compile(r"\bi can'?t\b", re.I),
    re.compile(r"\bnot (something|able to)\b", re.I),
    re.compile(r"\bno (route|path|capability) (for|to)\b", re.I),
    re.compile(r"\bcapabilit\w* unavailable\b", re.I),
]


def read_audit(path: Path) -> list[dict]:
    rows = []
    if not path.is_file():
        return rows
    for line in path.read_text().splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            rows.append(e)
    return rows


def read_ledger(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text())
    except ValueError:
        return []
    return [t for t in (data.get("recent_turns") or []) if isinstance(t, dict)]


def mine_failed_receipts(audit: list[dict]) -> list[dict]:
    out = []
    for e in audit:
        if e.get("ok"):
            continue
        dec = str(e.get("decision") or "")
        if dec in ("PARKED", "PREPARE_ONLY"):
            continue  # normal states, not failures
        if dec in ("DENY",):
            out.append({"source": "denied", "policy": dec,
                        "receipts": f"{e.get('lane')}/{e.get('op')}",
                        "trace_ref": str(e.get("turn_id") or "")})
        else:
            out.append({"source": "failed-receipt", "policy": dec,
                        "receipts": f"{e.get('lane')}/{e.get('op')} ok=false",
                        "trace_ref": str(e.get("turn_id") or "")})
    return out


def mine_undos(audit: list[dict], window_s: float = 600.0) -> list[dict]:
    undos = [e for e in audit if str(e.get("op") or "").lower() == "undo"]
    out = []
    for u in undos:
        try:
            ut = float(u.get("t") or 0)
        except (TypeError, ValueError):
            continue
        th = str(u.get("target_hash") or "")
        prior = [e for e in audit
                 if ts(e) and 0 < ut - ts(e) <= window_s
                 and str(e.get("op") or "").lower() != "undo"]
        match = next((e for e in prior if str(e.get("target_hash") or "") == th), None)
        out.append({"source": "undo", "policy": str(u.get("decision") or ""),
                    "receipts": f"undo of {match.get('lane') + '/' + match.get('op') if match else 'unknown action'} "
                                f"within {window_s / 60:.0f}min",
                    "trace_ref": str(u.get("turn_id") or "")})
    return out


def ts(e: dict) -> float:
    try:
        return float(e.get("t") or 0)
    except (TypeError, ValueError):
        return 0.0


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9']+", s.lower())) - {"a", "the", "to", "it", "please"}


def overlap(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def mine_ledger(turns: list[dict]) -> list[dict]:
    out = []
    prev = None
    for t in turns:
        owner = str(t.get("owner") or "")
        kind = str(t.get("kind") or "")
        evie = str(t.get("evie") or "")
        if kind in CANCEL_KINDS:
            out.append({"source": "cancelled", "utterance": owner,
                        "policy": kind, "receipts": evie[:200]})
        elif kind in FAIL_KINDS:
            out.append({"source": "failed-turn", "utterance": owner,
                        "policy": kind, "receipts": evie[:200]})
        if any(rx.search(owner) for rx in CORRECTION_RES):
            out.append({"source": "correction", "utterance": owner, "policy": kind})
        if prev is not None:
            try:
                dt = (datetime.fromisoformat(str(t.get("at"))) -
                      datetime.fromisoformat(str(prev.get("at")))).total_seconds()
            except ValueError:
                dt = -1
            if 0 <= dt <= 120 and overlap(owner, str(prev.get("owner") or "")) >= 0.6:
                out.append({"source": "rephrase", "utterance": owner,
                            "policy": f"overlap-repeat after {dt:.0f}s"})
        prev = t
    return out


def mine_capability_gaps(turns: list[dict]) -> list[dict]:
    """Unmet asks: failure-ish kinds or cant-do responses (frontier input)."""
    out = []
    for t in turns:
        owner, evie, kind = (str(t.get("owner") or ""), str(t.get("evie") or ""),
                             str(t.get("kind") or ""))
        if not owner.strip():
            continue
        if kind in FAIL_KINDS or any(rx.search(evie) for rx in CANT_RES):
            out.append({"utterance": owner, "kind": kind, "at": str(t.get("at") or ""),
                        "evie": evie[:200]})
    return out
