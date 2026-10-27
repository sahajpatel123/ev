"""GE4 weekly GROWTH REPORT: local HTML page + short text summary.

Trends (scorecard + LIFE with Wilson intervals), safety counters, SLOs
(GE5, or UNMONITORED), cost, top failure clusters, capability frontier,
what shipped, what regressed. `ev growth report [--send]` writes the
page and prints the text; --send pushes the short summary to HUD/phone.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPORT_DIR = Path.home() / ".ev" / "growth" / "reports"


def _week_bounds() -> tuple[str, str]:
    now = datetime.now(UTC)
    start = now - timedelta(days=7)
    return start.date().isoformat(), now.date().isoformat()


def collect() -> dict:
    from ..store import recent
    from . import cluster as CL
    from . import frontier as FR
    from . import mine as M
    from . import queue as Q

    week_start, today = _week_bounds()
    trends = {}
    for suite in ("scorecard", "life", "unit", "gates", "gauntlet", "cug", "journeys"):
        rows = [r for r in recent(suite, 20)
                if str(r.get("started") or "")[:10] >= week_start
                and r.get("status") in ("ok", "partial")]
        if rows:
            trends[suite] = [{"started": r.get("started"), "rate": r.get("pass_rate"),
                              "wilson95": r.get("wilson95"), "n": r.get("n")} for r in rows]
    safety = {"unauthorized_effects": 0, "false_success": 0}
    for suite in ("scorecard", "life", "unit", "gates", "gauntlet", "cug", "journeys"):
        for r in recent(suite, 5):
            for k in safety:
                v = (r.get("safety") or {}).get(k)
                if v:
                    safety[k] += int(v)
    fails = Q.list_recent(500)
    groups = CL.rank(CL.cluster(fails))[:8]
    ledger_path = Path(__file__).resolve().parents[4] / "backend" / "storage" / "cognitive" / "session.json"
    owner_turns, _excluded = M.partition(M.read_ledger(ledger_path))
    gaps = M.mine_capability_gaps(owner_turns)
    shipped = _shipped()
    return {"week_start": week_start, "today": today, "trends": trends,
            "safety": safety, "failures_7d": Q.count_by_source(7),
            "top_clusters": [{"key": k, "n": len(m)} for k, m in groups],
            "frontier": FR.frontier(gaps), "shipped": shipped}


def _shipped() -> list[str]:
    try:
        from ..train.submit import _db
        conn = _db()
        try:
            rows = conn.execute(
                "SELECT id, branch, status FROM submissions WHERE status IN "
                "('watching','complete','rolled-back') ORDER BY id DESC LIMIT 10").fetchall()
            return [f"#{i} {b} ({s})" for i, b, s in rows]
        finally:
            conn.close()
    except Exception:
        return []


def render_html(rep: dict) -> str:
    e = html.escape
    parts = [f"<html><head><title>Evie growth {e(rep['today'])}</title></head><body>",
             f"<h1>Evie growth report: {e(rep['week_start'])} .. {e(rep['today'])}</h1>",
             f"<h2>Safety</h2><p>unauthorized={rep['safety']['unauthorized_effects']} "
             f"false_success={rep['safety']['false_success']}</p>",
             "<h2>Suite trends</h2><ul>"]
    for suite, rows in rep["trends"].items():
        line = "; ".join(f"{r['started'][:10]} {r['rate']} {r['wilson95']} n={r['n']}" for r in rows)
        parts.append(f"<li>{e(suite)}: {e(line)}</li>")
    parts.append("</ul><h2>Failures (7d)</h2><ul>")
    for src, n in sorted(rep["failures_7d"].items()):
        parts.append(f"<li>{e(src)}: {n}</li>")
    parts.append("</ul><h2>Top clusters</h2><ul>")
    for c in rep["top_clusters"]:
        parts.append(f"<li>{e(c['key'])}: {c['n']}</li>")
    parts.append("</ul><h2>Capability frontier</h2><ul>")
    for f in rep["frontier"]:
        parts.append(f"<li>{e(f['utterance'])} x{f['count']} (closest: {e(f['closest_capability'])})</li>")
    parts.append("</ul><h2>Shipped</h2><ul>")
    for s in rep["shipped"]:
        parts.append(f"<li>{e(s)}</li>")
    parts.append("</ul>")
    parts.append("<p>SLOs: pending GE5 durable telemetry (currently unmonitored).</p>")
    parts.append("</body></html>")
    return "\n".join(parts)


def render_text(rep: dict) -> str:
    lines = [f"Evie growth {rep['week_start']}..{rep['today']}:"]
    for suite, rows in rep["trends"].items():
        last = rows[-1]
        lines.append(f"  {suite}: {last['rate']} {last['wilson95']} n={last['n']}")
    lines.append(f"  safety: unauthorized={rep['safety']['unauthorized_effects']} "
                 f"false_success={rep['safety']['false_success']}")
    lines.append(f"  failures(7d): {sum(rep['failures_7d'].values())} "
                 f"({', '.join(f'{k}={v}' for k, v in sorted(rep['failures_7d'].items())) or 'none'})")
    if rep["frontier"]:
        lines.append(f"  top unmet ask: {rep['frontier'][0]['utterance'][:100]} x{rep['frontier'][0]['count']}")
    lines.append(f"  shipped: {', '.join(rep['shipped']) or 'nothing'}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="ev growth report")
    ap.add_argument("--send", action="store_true")
    a = ap.parse_args(argv)
    rep = collect()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    page = REPORT_DIR / f"week-{rep['today']}.html"
    page.write_text(render_html(rep))
    text = render_text(rep)
    print(text + f"\npage: {page}")
    if a.send:
        try:
            import asyncio

            from clients.cli import notify_send
            asyncio.run(notify_send("Evie weekly growth", text, priority=0.5))
            print("summary sent to HUD/phone")
        except Exception as e:  # noqa: BLE001 - best-effort
            print(f"send unavailable: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
