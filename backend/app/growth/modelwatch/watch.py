"""GE7 model watch: weekly newer-version checks + bake-off flow.

Families (the two-model constraint, A9): Muse Spark via OpenRouter,
Gemini Live via Google. Listings are $0 read-only fetches; bake-offs
spend the eval key. Never switches without the owner (a setting change
through `ev release ship`).
"""

from __future__ import annotations

import json
import re
import urllib.request
from datetime import UTC, datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS modelwatch(
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, family TEXT NOT NULL,
  pinned TEXT NOT NULL, seen TEXT NOT NULL DEFAULT '[]',
  newer TEXT NOT NULL DEFAULT '[]', bakeoff TEXT NOT NULL DEFAULT '');
"""

OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"
GOOGLE_MODELS = "https://generativelanguage.googleapis.com/v1beta/models"

SPARK_RE = re.compile(r"muse-spark-([\d.]+)", re.I)
GEMINI_LIVE_RE = re.compile(r"gemini-([\d.]+).*live|live.*gemini-([\d.]+)", re.I)


def _db():
    from ..store import _connect

    conn = _connect()
    conn.executescript(SCHEMA)
    return conn


def _get(url: str, key: str = "") -> tuple[int, dict]:
    try:
        req = urllib.request.Request(url, headers=(
            {"Authorization": f"Bearer {key}"} if key else {}))
        with urllib.request.urlopen(req, timeout=25) as resp:
            return resp.status, json.load(resp)
    except Exception as e:  # noqa: BLE001 - network failure is data
        return -1, {"error": f"{type(e).__name__}: {e}"}


def _vtuple(s: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", s)[:3])


def pins() -> dict[str, str]:
    from app.config import settings
    try:
        from app.gateway.openrouter_muse import default_brain_model
        brain = default_brain_model()
    except Exception:
        brain = str(getattr(settings, "brain_model", ""))
    return {"spark": brain,
            "gemini-live": str(getattr(settings, "gemini_live_model", ""))}


def openrouter_spark_ids() -> tuple[list[str], str]:
    status, body = _get(OPENROUTER_MODELS)
    if status != 200 or not isinstance(body, dict):
        return [], f"OpenRouter listing unreadable (HTTP {status})"
    ids = [m.get("id", "") for m in body.get("data") or []
           if "muse-spark" in str(m.get("id", "")).lower() or "muse/spark" in str(m.get("id", "")).lower()]
    return sorted(set(ids)), ""


def google_live_ids() -> tuple[list[str], str]:
    from app.config import settings
    key = str(getattr(settings, "google_api_key", "") or "")
    if not key:
        return [], "no Google key (listing unverifiable; owner key state)"
    ids: set[str] = set()
    token = ""
    for _ in range(10):
        url = GOOGLE_MODELS + "?pageSize=1000" + (f"&pageToken={token}" if token else "")
        try:
            req = urllib.request.Request(url, headers={"x-goog-api-key": key})
            with urllib.request.urlopen(req, timeout=25) as resp:
                body = json.load(resp)
        except Exception as e:  # noqa: BLE001 - network failure is data
            return [], f"Google listing unreadable ({type(e).__name__})"
        if not isinstance(body, dict):
            return [], "Google listing unreadable (non-JSON)"
        for m in body.get("models") or []:
            methods = m.get("supportedGenerationMethods") or []
            if "bidiGenerateContent" in methods:
                ids.add(str(m.get("name", "")).split("/")[-1])
        token = body.get("nextPageToken") or ""
        if not token:
            break
    return sorted(ids), ""


def newer_than(pinned: str, seen: list[str], family: str) -> list[str]:
    """Family members with a higher version tuple than pinned (heuristic)."""
    pv = _vtuple(pinned)
    out = []
    for mid in seen:
        if mid == pinned:
            continue
        if family == "spark" and not SPARK_RE.search(mid):
            continue
        # gemini-live ids are pre-filtered to bidiGenerateContent-capable.
        if _vtuple(mid) > pv:
            out.append(mid)
    return sorted(out)


def check() -> dict:
    """Run the weekly check. Returns the report (also stored)."""
    from ..result import utcnow
    p = pins()
    spark_ids, spark_err = openrouter_spark_ids()
    live_ids, live_err = google_live_ids()
    rep = {"ts": utcnow(), "pins": p,
           "spark": {"seen": spark_ids, "error": spark_err,
                     "pinned_listed": p["spark"] in spark_ids,
                     "newer": newer_than(p["spark"], spark_ids, "spark")},
           "gemini-live": {"seen": live_ids, "error": live_err,
                           "pinned_listed": p["gemini-live"] in live_ids,
                           "newer": newer_than(p["gemini-live"], live_ids, "gemini-live")}}
    conn = _db()
    try:
        for fam in ("spark", "gemini-live"):
            conn.execute("INSERT INTO modelwatch(ts, family, pinned, seen, newer) VALUES(?,?,?,?,?)",
                         (rep["ts"], fam, p[fam], json.dumps(rep[fam]["seen"]),
                          json.dumps(rep[fam]["newer"])))
        conn.commit()
    finally:
        conn.close()
    return rep


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="ev growth modelwatch")
    ap.add_argument("--bakeoff", default=None, help="model id to bake off vs pinned")
    ap.add_argument("--budget", type=float, default=2.0)
    a = ap.parse_args(argv)
    if a.bakeoff:
        from .bakeoff import run_bakeoff
        rep = run_bakeoff(a.bakeoff, budget=a.budget)
        print(json.dumps(rep, indent=1)[:3000])
        return 0 if rep.get("ok") else 1
    rep = check()
    for fam in ("spark", "gemini-live"):
        r = rep[fam]
        listed = "listed" if r["pinned_listed"] else "PINNED-MISSING"
        print(f"{fam}: pinned={rep['pins'][fam]} seen={len(r['seen'])} {listed} "
              f"newer={r['newer'] or 'none'} {r['error'] or ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
