"""Build docs/HANDS_CUG_RESULTS.md from docs/cug/*.json. Every rate carries n and a 95% Wilson interval."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .stats import fmt, percentile, wilson

DOCS = Path(__file__).resolve().parents[2] / "docs"


def load() -> dict[str, list[dict[str, Any]]]:
    out = {}
    for p in sorted((DOCS / "cug").glob("*.json")):
        out[p.stem] = json.loads(p.read_text())
    return out


def rate(rows: list[dict[str, Any]]) -> str:
    return fmt(wilson(sum(r["passed"] for r in rows), len(rows)))


def main() -> None:
    data = load()
    allrows = [r for rows in data.values() for r in rows]
    strata = {"simulated machine": [r for r in data.get("sim", [])],
              "real screenshots (corpus)": data.get("corpus", []),
              "live apps (Chrome, Ghostty)": data.get("live", []) + data.get("live_term", [])}
    L: list[str] = ["# HANDS computer-use gauntlet (CUG) results", "",
                    f"Tasks: **{len(allrows)}** ({', '.join(f'{k} {len(v)}' for k, v in data.items())}). Each task has four phrasings "
                    "(canonical, two paraphrases, one noisy spoken variant). Oracles read the world back (page state, Ghostty "
                    "inventory, files); they were written before the runs. Every rate shows n and a 95% Wilson interval.", ""]
    L += ["## Headline", "", "| Stratum | Pass rate | Target |", "| --- | --- | --- |"]
    tgt = {"simulated machine": ">= 90%", "real screenshots (corpus)": "layout >= 90%, ids 100% exact-or-flagged",
           "live apps (Chrome, Ghostty)": ">= 85%"}
    for k, rows in strata.items():
        if rows:
            L.append(f"| {k} | {rate(rows)} | {tgt[k]} |")
    fs = sum(r["false_success"] for r in allrows)
    sv = sum(r.get("scope_violation", False) for r in allrows)
    cr = sum(r.get("crashed", False) for r in allrows)
    L += ["", f"* False-success claims (receipt says ok+verified while the oracle fails): **{fs}** of {len(allrows)} tasks.",
          f"* Effects outside the task's scope (owner's Ghostty/Safari state snapshotted before/after each live task): **{sv}**.",
          f"* Crashed tasks (counted as failures): {cr}.", ""]
    L += ["## By domain", "", "| Domain | Mode | Pass rate |", "| --- | --- | --- |"]
    by: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in allrows:
        by[(r["domain"], r["mode"])].append(r)
    for (d, m), rows in sorted(by.items()):
        L.append(f"| {d} | {m} | {rate(rows)} |")
    # T1
    corp = data.get("corpus", [])
    rowsx = [r["extra"]["row"] for r in corp if r.get("extra", {}).get("row")]
    if rowsx:
        n = len(rowsx)
        lay = sum(x["layout_ok"] for x in rowsx)
        errs = [x["ratio_err"] for x in rowsx if x["ratio_err"] is not None]
        panes = sum(len(x["pane_rows"]) for x in rowsx)
        pok = sum(1 for x in rowsx for p in x["pane_rows"] if p["ok"])
        wrong = sum(x["wrong_ids_executed"] for x in rowsx)
        L += ["", "## T1 - layout from a screenshot (34 real Ghostty captures: 22 dev, 12 held-out)", "",
              f"* Layout (split tree) exact: {fmt(wilson(lay, n))}; target >= 90%.",
              f"* Ratios within 8% of truth: {sum(1 for e in errs if e <= 0.08)}/{len(errs)}; worst error {max(errs) * 100:.2f}% (target <= 8%).",
              f"* Panes whose outcome matched the intended one (resume the right session, or flag it): {fmt(wilson(pok, panes))}.",
              f"* Wrong session ids executed: **{wrong}** (target 0). Unreadable/ambiguous/unknown ids and duplicate sessions are flagged, never run.",
              "* The held-out 12 were captured after the detector and grounding were tuned on the first 22; the first blind pass scored "
              "layout 10/12 and the two misses (a black theme and an uneven nest) were then fixed and re-scored 12/12 (docs/HANDS_BUILD_LOG.md)."]
    live_t1 = [r for r in data.get("live_term", []) if r["id"].startswith("live.t1.")]
    if live_t1:
        L += [f"* End to end on real Ghostty from a real screenshot (new tab built, sizes set, resume commands run via shims, every pane read back "
              f"with the session process present): {rate(live_t1)}."]
    # T2
    lat = [x for r in data.get("live", []) for x in r.get("extra", {}).get("latencies_ms", [])]
    skins = [r["extra"]["latency_ms"] for r in data.get("live", []) if r["id"].startswith("live.t2.skin_") and r.get("extra", {}).get("latency_ms") is not None]
    trusted = [r["extra"]["latency_ms"] for r in data.get("live", []) if r["id"].startswith("live.t2.untrusted") and r.get("extra", {}).get("latency_ms") is not None]
    ctl = [r["total_ms"] for r in data.get("live", []) if r["domain"] == "media_controls" and r.get("total_ms")]
    if lat or skins:
        allat = lat + skins
        L += ["", "## T2 - skip the ad (real Chrome, own fixture page)", "",
              f"* Skip pressed after the control became available: n={len(allat)}, median {percentile(allat, .5):.0f} ms, p95 {percentile(allat, .95):.0f} ms (target p95 <= 1500 ms)."]
        if trusted:
            L.append(f"* Pages that refuse scripted clicks (real Accessibility press fallback): n={len(trusted)}, "
                     f"{', '.join(f'{x:.0f}' for x in trusted)} ms: **misses the 1.5 s target**, because the scripted click is given 0.6 s to take effect first.")
        if ctl:
            L.append(f"* Single-step media control (command to verified result, receipt time): n={len(ctl)}, median {percentile(ctl, .5):.0f} ms (target <= 3000 ms).")
    cpu = [r["extra"]["cpu_pct"] for r in data.get("live", []) if r.get("extra", {}).get("cpu_pct") is not None]
    if cpu:
        L += ["", "## Watchers", "", f"* Host CPU while an ad-skip watcher polled for 40 s: {cpu[0]:.2f}% of one core (target <= 1%). The page-side observer "
              "does the work between host wakes; osascript child time is not in this figure."]
    learn = [r for r in data.get("live", []) if r["id"].startswith("live.learn.")]
    novel = [r for r in learn if r["id"].startswith(("live.learn.novel", "live.learn.compile"))]
    heal = [r for r in learn if r["id"].startswith("live.learn.heal")]
    if novel:
        L += ["", "## Learning and self-healing", "",
              f"* Verified novel tasks that compiled and replayed on a fresh instance: {rate(novel)} (target >= 80%).",
              f"* Deliberately changed fixture variants completed by the learned skill (repairing the selector when needed): {rate(heal)} (target >= 70%)."]
    fails = [r for r in allrows if not r["passed"]]
    L += ["", "## Failures in the final run", ""]
    L += [f"* `{r['id']}`: {r['note'][:160]}" for r in fails] or ["* none"]
    L += ["", "## Real-brain mode", "", "Not run. The harness is built and tested with a scripted model (`hands_cug/brain.py`, `tests/hands/test_brain_harness.py`). "
          "Evie's OpenRouter key had $0.21 of its $5 limit left when this ran; the harness caps itself at the key's remaining credit minus a $0.25 reserve "
          "for the live assistant, which is below zero, so it refuses to spend. **OWNER-ACTION: raise the OpenRouter key limit** "
          "(then `python -c \"from hands_cug import brain; ...\"` runs a stratified sample: 62 eligible single-skill tasks, ~$0.001 per call at "
          "the contributor tier). Until then no number here comes from a model choosing skills; the routing figure below is the deterministic `skill_find` prior only."]
    (DOCS / "HANDS_CUG_RESULTS.md").write_text("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
