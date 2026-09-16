"""Prove the Evie file sandbox end-to-end: brain plans, tests, runs.

Offline-safe: without a Muse key the brain degrades honestly
(degraded=True) and the deterministic parser still drives the same jail.

Usage:
  cd backend
  EV_MASTER_KEY=local-dev-key EV_VAULT_KEY=$(openssl rand -base64 48) \\
    EV_LAPTOP_FILES=true EV_FILE_SANDBOX_AUTONOMY=auto \\
    uv run python ../scripts/brain_file_sandbox_demo.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))


async def main() -> int:
    from app.config import settings

    tmp = Path(tempfile.mkdtemp(prefix="evie-sandbox-demo-"))
    settings.laptop_files = True  # type: ignore[attr-defined]
    settings.laptop_files_root = str(tmp)  # type: ignore[attr-defined]
    os.environ["EV_FILE_SANDBOX_AUTONOMY"] = "auto"

    from app.ev import brain_file_runner, file_sandbox

    print(f"jail: {tmp}")
    print(f"discover: {file_sandbox.discover(origin='mac')['spoken']}")
    print(f"index: {file_sandbox.index_status(rebuild=True, origin='mac')}")

    wrote = file_sandbox.execute_op(
        "write", {"path": str(tmp / "demo.txt"), "content": "milk\neggs\n"},
        origin="mac", confirm=True,
    )
    print(f"write: ok={wrote['ok']} path={wrote.get('path')}")

    # Brain tests first (dry-run), then runs for real — one call.
    result = await brain_file_runner.test_brain_command("find the demo file", origin="iphone")
    print(f"brain test: model={result['model']} degraded={result['degraded']} ok={result['ok']}")
    print(f"  test ops: {(result['test'].get('ops') or [])}")
    if result.get("result"):
        print(f"  real ok: {result['result']['ok']}")

    ran = file_sandbox.execute_op("run" if False else "search", {"query": "demo"}, origin="mac")
    print(f"search: {ran.get('spoken')} hits={ran.get('count')}")

    undone = file_sandbox.undo(origin="mac")
    print(f"undo (expect nothing-or-restore): ok={undone['ok']} error={undone.get('error')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
