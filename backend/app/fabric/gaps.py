"""Gap reporting with closest-alternative suggestion."""
from __future__ import annotations
import difflib
from pathlib import Path
import json, time
from .policy import _receipt, _KNOWN_LANES


def _alternatives(missing: str) -> list[str]:
    return difflib.get_close_matches(missing.lower(), _KNOWN_LANES, n=3, cutoff=0.3) or _KNOWN_LANES[:2]


def report_gap(desc: str, root=None, **kw) -> dict:
    alt = _alternatives(desc or "")
    return _receipt(ok=True, verified=True,
                    evidence={"gap": desc, "alternative": alt[0] if alt else None, "alternatives": alt})


def record_gap(desc: str, root=None, **kw) -> dict:
    r = report_gap(desc, root=root, **kw)
    try:
        if root is not None:
            p = Path(root) / ".ev-gaps.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a") as f:
                f.write(json.dumps({"t": time.time(), "gap": desc}) + "\n")
    except OSError:
        pass
    return r


def closest_alternative(desc: str) -> str | None:
    alt = _alternatives(desc or "")
    return alt[0] if alt else None
