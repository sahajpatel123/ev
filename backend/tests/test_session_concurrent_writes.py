"""Two processes share one durable session; neither may erase the other.

Mac Talk runs the voice edge on its own port and the API runs the kernel on
another, so a live turn and a typed turn can hold the session at the same
time. The turn ledger is what lets a bare "yes" resolve to the question Evie
asked, so losing a row loses the owner's answer.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_CHILD = r'''
import os, sys, time
root, gate, role = sys.argv[1], sys.argv[2], sys.argv[3]
os.environ["EV_STORAGE_ROOT"] = root
os.environ.setdefault("EV_MASTER_KEY", "k")
os.environ.setdefault("EV_VAULT_KEY", "x" * 44)
sys.path.insert(0, {backend!r})
from app.cognitive.intent import remember_exchange
from app.cognitive.session_store import current, forget_live_cache

forget_live_cache()
row = current()                      # this process's snapshot starts here
open(os.path.join(gate, role + ".ready"), "w").close()
while not os.path.exists(os.path.join(gate, "go")):
    time.sleep(0.01)

if role == "other":
    remember_exchange(row, owner="from the other surface", assistant="ack")
    open(os.path.join(gate, "other.done"), "w").close()
else:
    while not os.path.exists(os.path.join(gate, "other.done")):
        time.sleep(0.01)
    # Still holding the pre-write snapshot: this save must not erase theirs.
    remember_exchange(row, owner="from this surface", assistant="ack")
'''


def test_concurrent_processes_do_not_erase_each_others_rows(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    gate = tmp_path / "gate"
    gate.mkdir()
    child = tmp_path / "child.py"
    child.write_text(
        _CHILD.format(backend=str(Path(__file__).resolve().parents[1])),
        encoding="utf-8",
    )

    env = dict(os.environ, EV_STORAGE_ROOT=str(root))
    procs = {
        role: subprocess.Popen(
            [sys.executable, str(child), str(root), str(gate), role],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        for role in ("other", "mine")
    }
    try:
        for _ in range(2000):
            if all((gate / f"{role}.ready").exists() for role in procs):
                break
            time.sleep(0.005)
        else:  # pragma: no cover - harness failure, not product behaviour
            pytest.fail("children never reached the barrier")
        (gate / "go").write_text("", encoding="utf-8")
        output = {role: proc.communicate(timeout=90)[0] for role, proc in procs.items()}
    finally:
        for proc in procs.values():
            if proc.poll() is None:  # pragma: no cover - cleanup only
                proc.kill()
    for role, text in output.items():
        assert procs[role].returncode == 0, f"{role} failed: {text}"

    session = root / "cognitive" / "session.json"
    assert session.exists(), "no session file was written"
    kept = [
        row.get("owner")
        for row in json.loads(session.read_text(encoding="utf-8")).get("recent_turns", [])
    ]
    assert "from the other surface" in kept, f"a concurrent write was erased: {kept}"
    assert "from this surface" in kept, kept
