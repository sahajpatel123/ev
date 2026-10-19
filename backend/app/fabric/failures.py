"""Redacted queue of turns that did not verify.

Secrets are stripped before anything is written. Repair briefs read this
queue. Recording a failure does not change lane behavior.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

_SECRET = re.compile(r"(?i)(api[_-]?key|token|secret|password|bearer)\s*[:=]\s*\S+")


def queue_path() -> Path:
    from app.config import settings

    raw = str(getattr(settings, "fabric_failure_queue", "") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".ev" / "failure_queue.jsonl"


def redact(text: str) -> str:
    return _SECRET.sub(r"\1=[redacted]", text or "")


def record(utterance: str, receipt: dict, *, trace: dict | None = None,
           source: str = "turn") -> dict:
    """Append one redacted failure. Returns the record that was written."""
    entry = {
        "ts": time.time(),
        "source": source,
        "utterance": redact(utterance or ""),
        "ok": bool(receipt.get("ok")),
        "verified": bool(receipt.get("verified")),
        "error": redact(str(receipt.get("error") or "")),
        "trace": trace or {},
        "status": "open",
    }
    path = queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
    return entry


def read_queue(path: Path | None = None) -> list[dict]:
    target = path or queue_path()
    if not target.is_file():
        return []
    out = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
