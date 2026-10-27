"""GE5 "Evie Health": local SLO dashboard page. `ev growth health`."""

from __future__ import annotations

import html
from datetime import UTC, datetime
from pathlib import Path

PAGE = Path.home() / ".ev" / "growth" / "health.html"


def render(rep: dict) -> str:
    e = html.escape
    rows = []
    for name, v in rep["slos"].items():
        val = "UNMEASURED" if v["value"] is None else str(v["value"])
        cls = "breach" if v["breached"] else ("unmeasured" if v["value"] is None else "ok")
        rows.append(f'<tr class="{cls}"><td>{e(name)}</td><td>{e(val)}</td>'
                    f'<td>{e(str(v["target"]))}</td><td>{v["n"]}</td></tr>')
    return f"""<html><head><title>Evie Health</title>
<style>body{{font-family:sans-serif;max-width:700px;margin:2em auto}}
.breach{{background:#fdd}} .ok{{background:#dfd}} .unmeasured{{background:#eee}}
td,th{{padding:4px 10px;text-align:left}}</style></head><body>
<h1>Evie Health</h1><p>measured {e(datetime.now(UTC).isoformat(timespec='seconds'))};
turns: {rep['n_7d']}/7d, {rep['n_24h']}/24h</p>
<table><tr><th>SLO</th><th>measured</th><th>target</th><th>n</th></tr>
{''.join(rows)}</table>
<p>UNMEASURED = no data yet (telemetry hook lands with the release train;
never green-by-default).</p></body></html>
"""


def main(argv: list[str] | None = None) -> int:
    from .alerts import current
    rep = current()
    PAGE.parent.mkdir(parents=True, exist_ok=True)
    PAGE.write_text(render(rep))
    print(f"Evie Health: {PAGE} ({rep['n_7d']} turns/7d)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
