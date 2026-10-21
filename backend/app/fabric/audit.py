"""Tamper-evident audit log (hash chain) for every fabric effect decision.

One JSONL file (default ``~/.ev/fabric_audit.jsonl``, overridable via
``EV_FABRIC_AUDIT_FILE`` for tests). Each entry links the previous hash, so
any edit, deletion, or reorder breaks :func:`verify`. Targets are stored as
SHA-256 hashes only — raw commands/paths may carry secrets (R3). Appends
take an exclusive file lock so concurrent turns cannot interleave.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("ev.fabric.audit")

_GENESIS = "GENESIS"


def _path() -> Path:
    from app.config import settings

    raw = str(getattr(settings, "fabric_audit_file", "") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".ev" / "fabric_audit.jsonl"


def _entry_hash(prev: str, seq: int, t: float, lane: str, op: str,
                target_hash: str, decision: str, ok: bool) -> str:
    body = "|".join([prev, str(seq), repr(t), lane, op, target_hash, decision,
                     "1" if ok else "0"])
    return hashlib.sha256(body.encode()).hexdigest()


def record(lane: str, op: str, target: str = "", decision: str = "",
           ok: bool = False, turn_id: str | None = None) -> dict[str, Any]:
    """Append one entry. Never raises: audit failure warns, never breaks effects."""
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        target_hash = hashlib.sha256(str(target or "").encode()).hexdigest()
        with open(path, "a+") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.seek(0)
                lines = [line for line in fh.read().splitlines() if line.strip()]
                prev_entry = json.loads(lines[-1]) if lines else None
                seq = int(prev_entry.get("seq", 0)) + 1 if prev_entry else 1
                prev_hash = str(prev_entry.get("hash", _GENESIS)) if prev_entry else _GENESIS
                t = time.time()
                entry = {
                    "seq": seq, "t": t, "lane": lane, "op": op,
                    "target_hash": target_hash, "decision": decision,
                    "ok": bool(ok), "prev": prev_hash,
                    "hash": _entry_hash(prev_hash, seq, t, lane, op,
                                        target_hash, decision, bool(ok)),
                }
                if turn_id:
                    entry["turn_id"] = turn_id
                fh.seek(0, 2)
                fh.write(json.dumps(entry) + "\n")
                return {"ok": True, "seq": seq}
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        logger.warning("fabric audit append failed: %s", exc)
        return {"ok": False, "error": str(exc)}


def verify() -> dict[str, Any]:
    """Recompute the whole chain. Any edit, deletion, or reorder fails."""
    try:
        path = _path()
    except OSError as exc:
        return {"ok": False, "checked": 0, "error": str(exc)}
    if not path.exists():
        return {"ok": True, "checked": 0}
    try:
        with open(path) as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
            try:
                lines = [line for line in fh.read().splitlines() if line.strip()]
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        return {"ok": False, "checked": 0, "error": str(exc)}
    prev_hash = _GENESIS
    for index, line in enumerate(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            return {"ok": False, "checked": index, "error": f"line {index + 1} not JSON"}
        if int(entry.get("seq", -1)) != index + 1:
            return {"ok": False, "checked": index, "error": f"seq gap at line {index + 1}"}
        if str(entry.get("prev", "")) != prev_hash:
            return {"ok": False, "checked": index, "error": f"prev-link broken at line {index + 1}"}
        recomputed = _entry_hash(
            prev_hash, index + 1, float(entry.get("t", 0)),
            str(entry.get("lane", "")), str(entry.get("op", "")),
            str(entry.get("target_hash", "")), str(entry.get("decision", "")),
            bool(entry.get("ok", False)),
        )
        if recomputed != str(entry.get("hash", "")):
            return {"ok": False, "checked": index, "error": f"hash mismatch at line {index + 1}"}
        prev_hash = str(entry.get("hash"))
    return {"ok": True, "checked": len(lines)}
