"""Group eval losses by cause (runs on tune_*.json, no brain).

  .venv/bin/python eval/personality/analyze.py results/tune_round1.json
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


def primary_cause(item: dict) -> str:
    if item.get("hard_fails"):
        return "hard:" + "+".join(item["hard_fails"][:2])
    criteria = item.get("criteria", {})
    for cause, keys in (("truth-useful", ("truth", "usefulness")),
                        ("funny", ("funny",)),
                        ("specificity", ("specificity",)),
                        ("voice", ("human", "appropriateness"))):
        votes = [s for k in keys for s in criteria.get(k, [])]
        if votes and votes.count("baseline") > votes.count("v2"):
            return cause
    return "tie/unclear"


def main(argv: list[str]) -> int:
    payload = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    items = payload["items"]
    losses = [i for i in items if i["verdict"] != "v2"]
    groups: dict[str, list[dict]] = defaultdict(list)
    for item in losses:
        groups[primary_cause(item)].append(item)
    print(f"total={len(items)} wins={sum(1 for i in items if i['verdict']=='v2')} "
          f"losses+ties={len(losses)} "
          f"win_rate={payload.get('win_rate', 0):.3f} "
          f"cost=${payload.get('cost_usd', 0):.4f} calls={payload.get('calls', 0)}")
    print(f"per_judge={payload.get('per_judge_win_rate')} "
          f"agreement={payload.get('judge_agreement_rate')}")
    print(f"sanity={payload.get('sanity', {}).get('passed')} "
          f"{payload.get('sanity', {}).get('per_judge_failures')}")
    print(f"truth_useful_ok={payload.get('truth_useful_ok_rate', 0):.3f} "
          f"hard_fails={payload.get('hard_fails', 0)}")
    fams = Counter(i.get("family", "?") for i in losses)
    print(f"loss families: {dict(fams)}")
    for cause in sorted(groups, key=lambda c: -len(groups[c])):
        ids = [f"{i['id']}({i['verdict']},{i['score']:.2f})" for i in groups[cause]]
        print(f"\n## {cause} x{len(groups[cause])}\n  " + "\n  ".join(ids))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
