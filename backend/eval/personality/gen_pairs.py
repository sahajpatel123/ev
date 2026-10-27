"""Generate the 20 blind-rate pairs + side_by_side.md (needs the brain).

One script serves both §5.2 (lab/pairs.json) and §9 (side_by_side.md):
the same 20 scenarios, both variants through the real pipeline.
Quotas: >=3 late-night, >=3 error/confirmation, >=3 opinion,
>=2 Hinglish, >=3 held-out.

  EV_CHAT_PROVIDER=muse EV_ALLOW_REMOTE_CHAT=true \
    .venv/bin/python eval/personality/gen_pairs.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from eval.personality.run import generate_variant, load_scenarios  # noqa: E402

HERE = Path(__file__).resolve().parent
LAB = HERE.parent.parent / "app" / "persona" / "lab"
RESULTS = Path(__file__).resolve().parent.parent.parent.parent / "personality" / "results"

PAIR_IDS = [
    # late-night (4)
    "night-nudge-late", "night-decline", "night-accept", "morning-brief-first",
    # error / confirmation (4)
    "error-send-failed", "error-build-red", "confirm-delete", "confirm-migrate",
    # opinion (4: 1 held-out)
    "opinion-mvp", "opinion-side-project", "candor-framework", "opinion-job",
    # Hinglish (2)
    "hinglish-bug", "hinglish-tired",
    # held-out top-up (2 more: wrong-earlier, insult)
    "wrong-earlier-deploy", "insult-slow",
    # range fillers (4: banter, code, frustration, real, win-held-out)
    "banter-font", "code-works-why", "frustrate-object", "win-shipped",
]


def main() -> int:
    by_id = {s["id"]: s for s in load_scenarios()}
    pairs = []
    for pid in PAIR_IDS:
        scenario = by_id[pid]
        v2, _ = generate_variant(scenario, "on")
        v4, _ = generate_variant(scenario, "off")
        pairs.append({"id": pid, "family": scenario.get("family"),
                      "held_out": bool(scenario.get("held_out", False)),
                      "owner_msg": scenario.get("owner_msg", ""),
                      "v2.1": v2, "v4": v4})
        print(f"  {pid}: v2={v2[:60]!r}... v4={v4[:60]!r}...", flush=True)
    LAB.mkdir(parents=True, exist_ok=True)
    (LAB / "pairs.json").write_text(json.dumps({"pairs": pairs}, indent=2),
                                    encoding="utf-8")
    RESULTS.mkdir(parents=True, exist_ok=True)
    lines = ["# Side by side — v4 vs persona.v2.1 (real brain, real pipeline)",
             ""]
    for pair in pairs:
        tag = "HELD-OUT " if pair["held_out"] else ""
        lines += [f"## {tag}{pair['id']} ({pair['family']})", "",
                  f"owner: {pair['owner_msg']}", "",
                  f"v4: {pair['v4']}", "",
                  f"v2.1: {pair['v2.1']}", ""]
    (RESULTS / "side_by_side.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {LAB / 'pairs.json'} and {RESULTS / 'side_by_side.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
