"""Resource probe for the 24/7 ambient collector (RSS/CPU/queue bounds).

Samples ``ps`` for a target PID every ``--interval`` seconds for
``--duration`` seconds and appends JSONL rows to ``--out`` (default
``~/.ev/collector-resource.jsonl``).  Rows carry ``rss_kb``, ``rss_mb``,
``cpu_percent``, and (when ``--queue`` is given) ``queue_count`` /
``queue_bytes`` so the 24 h resource curve and the bounded-buffer claim can
be audited from one log.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path


def _sample(pid: int) -> dict | None:
    try:
        result = subprocess.run(
            ["ps", "-o", "rss=,%cpu=,etime=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    parts = result.stdout.strip().split()
    if len(parts) < 2:
        return None
    try:
        rss_kb = int(float(parts[0]))
        cpu_percent = float(parts[1])
    except ValueError:
        return None
    return {
        "rss_kb": rss_kb,
        "rss_mb": round(rss_kb / 1024.0, 2),
        "cpu_percent": cpu_percent,
        "etime": parts[2] if len(parts) > 2 else None,
    }


def _queue_summary(queue_dir: str | None) -> dict:
    if not queue_dir:
        return {"queue_count": None, "queue_bytes": None}
    path = Path(queue_dir) / "pending.jsonl"
    try:
        data = path.read_bytes()
        return {"queue_count": data.count(b"\n"), "queue_bytes": len(data)}
    except OSError:
        return {"queue_count": 0, "queue_bytes": 0}


def summarize(path: Path) -> dict:
    """Aggregate a resource JSONL into the curve numbers used for acceptance."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {"error": f"cannot read {path}"}
    rows = []
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    if not rows:
        return {"rows": 0}

    rss = [float(row["rss_mb"]) for row in rows if row.get("rss_mb") is not None]
    cpu = [float(row["cpu_percent"]) for row in rows if row.get("cpu_percent") is not None]
    queue = [int(row["queue_count"]) for row in rows if row.get("queue_count") is not None]
    alive = sum(1 for row in rows if row.get("alive") is True)
    report: dict = {
        "rows": len(rows),
        "start_ts": rows[0].get("ts"),
        "end_ts": rows[-1].get("ts"),
        "alive_rows": alive,
        "queue_max": max(queue) if queue else None,
    }
    if rss:
        report["rss_min_mb"] = round(min(rss), 2)
        report["rss_max_mb"] = round(max(rss), 2)
        report["rss_avg_mb"] = round(sum(rss) / len(rss), 2)
    if cpu:
        report["cpu_max"] = round(max(cpu), 2)
        report["cpu_avg"] = round(sum(cpu) / len(cpu), 3)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default=None, help="summarize a resource JSONL and exit")
    parser.add_argument("--pid", type=int, default=None, help="target PID (mutually exclusive with --pid-file)")
    parser.add_argument(
        "--pid-file",
        default=None,
        help="file containing the target PID (re-read every sample)",
    )
    parser.add_argument("--duration", type=float, default=86_400, help="total seconds to sample (default 24 h)")
    parser.add_argument("--interval", type=float, default=60, help="seconds between samples")
    parser.add_argument(
        "--out",
        default=str(Path.home() / ".ev" / "collector-resource.jsonl"),
        help="JSONL output path",
    )
    parser.add_argument("--queue", default=None, help="collector queue dir to include in each row")
    args = parser.parse_args()
    if args.report:
        print(json.dumps(summarize(Path(args.report)), sort_keys=True))
        return
    if args.pid is None and args.pid_file is None:
        parser.error("either --pid or --pid-file is required")
    if args.pid is not None and args.pid_file is not None:
        parser.error("--pid and --pid-file are mutually exclusive")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with out.open("a", encoding="utf-8") as handle:
        while time.monotonic() - started < args.duration:
            pid = args.pid
            if pid is None:
                try:
                    pid = int(Path(args.pid_file).read_text(encoding="utf-8").strip())
                except (OSError, ValueError):
                    pid = None
            sampled = _sample(pid) if pid is not None else None
            row = sampled or {}
            row["alive"] = sampled is not None
            row.update(_queue_summary(args.queue))
            row.update({"ts": datetime.now(UTC).isoformat(), "pid": pid})
            handle.write(json.dumps(row, sort_keys=True) + "\n")
            handle.flush()
            time.sleep(max(1.0, args.interval))


if __name__ == "__main__":
    main()
