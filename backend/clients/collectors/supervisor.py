"""Minimal supervisor for the 24/7 ambient collector endurance run.

Keeps ``python -m clients.collectors`` alive for a full 24 h acceptance
window: if the collector exits (crash, OOM kill, accidental signal), the
supervisor restarts it after a short pause and rewrites the PID file, so the
resource probe (which re-reads the PID file every sample) keeps tracking the
new process automatically.  The bounded offline queue lives on disk, so a
restart loses nothing and replay remains duplicate-free.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import TextIO


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=int, default=30, help="collector loop interval seconds")
    parser.add_argument(
        "--pid-file",
        default=str(Path.home() / ".ev" / "collector.pid"),
        help="PID file the resource probe reads",
    )
    parser.add_argument(
        "--log",
        default=str(Path.home() / ".ev" / "collector-supervisor.log"),
        help="collector stdout/stderr log (append)",
    )
    parser.add_argument(
        "--restart-delay",
        type=float,
        default=5.0,
        help="seconds to wait before restarting a crashed collector",
    )
    parser.add_argument(
        "--max-restarts",
        type=int,
        default=0,
        help="maximum restarts before giving up (0 = unlimited)",
    )
    args = parser.parse_args()

    env = dict(os.environ)
    backend_root = Path(__file__).resolve().parent.parent.parent
    env.setdefault("PYTHONPATH", str(backend_root))
    command = [
        sys.executable,
        "-u",
        "-m",
        "clients.collectors",
        "--interval",
        str(args.interval),
    ]
    pid_file = Path(args.pid_file)
    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def spawn() -> tuple[subprocess.Popen, TextIO]:
        handle = log_path.open("a", encoding="utf-8")
        process = subprocess.Popen(
            command,
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        pid_file.write_text(str(process.pid), encoding="utf-8")
        return process, handle

    process, handle = spawn()
    restarts = 0
    stopping = False

    def _stop(signum: int, frame: object) -> None:
        nonlocal stopping
        stopping = True
        process.terminate()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    while True:
        returncode = process.wait()
        handle.write(f"[supervisor] collector exited rc={returncode}\n")
        handle.flush()
        if stopping:
            handle.close()
            return
        if args.max_restarts and restarts >= args.max_restarts:
            handle.write("[supervisor] restart limit reached; giving up\n")
            handle.flush()
            handle.close()
            return
        restarts += 1
        time.sleep(max(1.0, args.restart_delay))
        process, handle = spawn()


if __name__ == "__main__":
    main()
