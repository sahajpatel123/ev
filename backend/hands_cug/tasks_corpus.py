"""CUG tasks over the real screenshot corpus (analysis only: layout, grounding, never a wrong id)."""

from __future__ import annotations

import json
from pathlib import Path

from .core import Verdict, task

ROOT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "hands" / "screens"
SAY = ["open a new tab with the same layout and resume those coding sessions",
       "look at my last screenshot and resume those sessions",
       "rebuild this terminal layout from the screenshot and resume the sessions",
       "open a new tab like my screenshot n resume them"]


def _one(kind: str, name: str):
    @task(f"corpus.t1.{kind}.{name}", "terminal_t1", "corpus", SAY, "coding_sessions.restore_from_screenshot",
          stratum="corpus", difficulty="hard")
    def run(env):  # env unused: pure analysis of a captured image
        from .t1_eval import EVV, evaluate

        if not EVV.exists():
            return Verdict(None, False, "the Apple Vision helper is not built")
        corpus = ROOT / kind
        seed = 11 if kind == "real" else 23
        res = evaluate("fast", names=[name], tiered=True, corpus=corpus, seed=seed)
        row = res["rows"][0]
        ok = row["layout_ok"] and row["panes_ok"] and row["wrong_ids_executed"] == 0
        return Verdict(None, bool(ok), f"layout={'ok' if row['layout_ok'] else 'BAD'} ratio_err={row['ratio_err']} "
                                      f"panes_ok={row['panes_ok']} wrong_ids={row['wrong_ids_executed']}",
                       {"row": row})

    return run


for _kind in ("real", "heldout"):
    for _p in sorted((ROOT / _kind).glob("*.json")):
        _one(_kind, json.loads(_p.read_text())["name"])
