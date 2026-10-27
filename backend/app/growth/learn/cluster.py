"""GE4 clustering: deterministic root-cause groups over the failure queue.

Cluster key = (source, lane/op head, policy/error head). No LLM: groups
are exact and reproducible; the nightly log shows every member. A cluster
with >= 2 members (or any denied/safety member) becomes a candidate case.
"""

from __future__ import annotations

import re


def cluster_key(f: dict) -> str:
    lane_op = (str(f.get("receipts") or "").split() or ["-"])[0][:60]
    pol = (str(f.get("policy") or "").split() or ["-"])[0][:40]
    lane_op = re.sub(r"[^a-zA-Z0-9_./-]+", "_", lane_op)
    return f"{f.get('source', '?')}::{lane_op}::{pol}"


def cluster(failures: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for f in failures:
        groups.setdefault(cluster_key(f), []).append(f)
    return groups


def rank(groups: dict[str, list[dict]]) -> list[tuple[str, list[dict]]]:
    def score(item: tuple[str, list[dict]]) -> tuple[int, str]:
        key, members = item
        safety = 100 if key.startswith(("denied::", "failed-receipt::")) else 0
        return (safety + len(members), members[-1].get("ts", ""))

    return sorted(groups.items(), key=score, reverse=True)
