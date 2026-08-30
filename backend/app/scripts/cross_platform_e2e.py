"""Automated cross-platform e2e without physical phones."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def main() -> int:
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "tests/test_device_gateway.py",
        "tests/test_device_gateway_convergence.py",
    ]
    extra = ROOT / "backend" / "tests" / "test_device_gateway_convergence.py"
    if not extra.exists():
        cmd = [sys.executable, "-m", "pytest", "-q", "tests/test_device_gateway.py"]
    return subprocess.call(cmd, cwd=ROOT / "backend")


if __name__ == "__main__":
    raise SystemExit(main())
