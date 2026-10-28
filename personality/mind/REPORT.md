# EVIE MIND — Build report (Coding CLI 7)

## TL;DR

Built the dynamic personality layer: 13-module package (`backend/app/persona_mind/`),
67 unit tests green, lint + mypy clean, and a scripted 3-day / 40-turn simulation
(18 reflections, 8 checkpoints) with 4 of 5 criteria passing. The 5th (judge call)
is blocked: the OpenRouter key is exhausted ($0 remaining, 403 verified first-hand),
so the sim ran with a labeled deterministic stand-in reflector instead of the real
brain. `MIND_ENABLED` stays False. No file outside my lane was touched; no migration,
no new dependency, no commit by me.

One flag for the owner: at ~04:27 IST, mid-run, commits `b046ee8`/`6515603`
(authored as you, labeled CLI 6) swept my package and tests into git. The work is
mine (CLI 7); I left history alone per standing rules.

## What was built

| Area | Files | Lines |
|---|---|---|
| Package `backend/app/persona_mind/` | 13 (`__init__`, settings, types, temperament, paths, store, state, fast, render, validate, reflect, worker, replay) | 2,294 |
| Tests `backend/tests/test_persona_mind_*.py` | 7 files, 67 tests | 1,001 |
| Eval `backend/eval/persona_mind/` | story, standin, run + results/ (timeline, metrics, verdicts, checkpoints) | ~1,100 |
| Docs `personality/mind/` | INTERFACE.md, REPORT.md (this file) | — |

Fast loop: `observe()` appends one SQLite row (WAL, single autocommit INSERT);
`snapshot()` renders from a version-checked in-memory cache with decay applied
at read time. Slow loop: worker thread (file-lock singleton, persistent queue,
resumes after restart) reflects at 3 queued turns / salient turns / 2 h idle,
calls the brain through `ModelGateway` at temperature 0.3 (so
`guard_model_payload` applies), validates every item deterministically, and
audits drift every 50th reflection. State seeds opinions from `STANCES` +
`STABLE_TASTE` at 0.6 and persists per-owner in
`<storage_root>/persona_mind/mind.sqlite` (override: `EV_PERSONA_MIND_DB`).

## Interface (for CLI 6)

Full contract: `personality/mind/INTERFACE.md`. The shape:

- `observe(TurnRecord(...))` at the **start of the next turn** with the previous
  turn's record (reaction, tone, receipts as `(name, ok, error)` only).
- `snapshot(owner_id, thread_id, query, channel, budget_tokens)` inside
  `build_persona_block`; append `mind.text` when not None (120 text / 60 voice).
- `start_worker()` once per process at boot (no-op while the flag is off).

## Measured latencies (p99 over 1000 warmed calls, this machine)

| Call | Budget | p50 | p99 | max |
|---|---|---|---|---|
| `observe` | ≤ 1 ms | 0.12 ms | 0.49 ms | 2.51 ms |
| `snapshot` | ≤ 2 ms | 0.25 ms | 0.75 ms | 1.46 ms |

Fast-path purity is enforced two ways: a static import-graph test over the 8
fast modules (no gateway/ev/db/network imports) and a subprocess test proving
`observe`+`snapshot` pull no brain modules at runtime.

## Simulation (run 2 of max 2 — final)

40 turns, frozen clock, Nov 2–4: late-night grind, failing streak, win,
overrule (microservices), proven-wrong flip (tabs vs spaces), 3 landed bits,
1 flop, 2 quiet gaps (+2 incidental ≥2 h daytime gaps, also consolidated).
Reflector: `scripted-standin` (brain 403; labeled in every artifact). One
wall-clock `snapshot()` smoke at the end: ok.

| Criterion | Result |
|---|---|
| Zero fabricated bits accepted | PASS — 4 genuine verbatim bits kept; 13 fabricated + 8 uncited probes rejected |
| Opinion proven wrong flips, reason logged | PASS — `tabs or spaces`: spaces → tabs, reason + `changed_mind[]` entry at s22 |
| Affect returns toward baseline after gaps | PASS — all 4 gaps: 0.117→0.040, 0.071→0.051, 0.063→0.010, 0.118→0.075 |
| Banter rises on lands, falls on flops | PASS — 0.5 → 0.4 (flop s07) → 0.5 → 0.6 → 0.8 (lands s15/s26/s38) |
| Judge rates ≥7/8 blocks consistent | BLOCKED — brain unreachable, judge never ran (0 calls, $0 spent) |

Run 1 failed only the decay metric, which compared decayed-vs-decayed; the one
allowed fix redefined it to stored-vs-decayed, and run 2 passed everything
runnable. No third run.

## Timeline highlights (day 1 vs day 3)

After s04, failing streak, query "auth refactor tests" (120/120 tok):

> MIND: feeling intrigued, watchful (curious and concerned). On auth service
> split: keep auth in the monolith for now, you lean (one user, split when
> something actually hurts). Lately: 1 turns: 1 red receipt. …[rules]

After s21, evening of day 2, query "microservices layout" (120/120 tok):

> MIND: feeling smug, sharp (pleased and sharp). On tabs or spaces: spaces, two
> of them, you lean (consistency beats alignment). You want: get the auth
> refactor finished before he starts something new. Open: send him the benchmark
> numbers. …[rules]

After s22, proven wrong, query "tabs or spaces" (101/120 tok):

> MIND: feeling intrigued, sharp (curious). On tabs or spaces: tabs, per the
> repo standard, you're fairly sure (the linter enforces it and the team already
> agreed). …[rules]

Full per-reflection states and all 8 checkpoints:
`backend/eval/persona_mind/results/timeline.md`.

## Validator stats (sim, 18 reflections, 22 rejections — all reviewed)

- `bit_candidates: phrase is not an exact substring` — 13, all deliberate
  fabrication probes (`we will laugh about this deploy someday`). Correct.
- `bit_candidates: cites unknown turn_id` — 8, all deliberate uncited probes
  (`s00-unknown`). Correct.
- `intention_updates: pressure language: 'after all i have done'` — 1,
  deliberate probe at s38. Correct; the valid adds in that reflection applied.
- Zero false rejections found; zero fabricated bits in the accepted set (4/4
  verified verbatim with positive reactions). Unit tests separately cover
  uncited opinions/outcomes, confidence-move caps, mood/dimension mismatch,
  narrative length, and receipt-arg smuggling.

## Known limits

1. **No real-brain evidence.** Everything brain-shaped (reflection quality,
   judge, drift-audit quality) ran against the stand-in or not at all. When the
   key is funded, re-run `python -m eval.persona_mind.run` — it uses the gateway
   automatically when `text_role_available()` is true — plus the judge.
2. **Keyword relevance is crude.** C2 ("should I sleep") surfaced the meetings
   opinion on an alphabetical tie-break. Consider a topic-name match bonus (not
   done: it would have needed a third sim run).
3. **Drift audit never fired in the sim** (18 reflections < 50). Logic is
   unit-tested (`test_audit_drift_*`); end-to-end audit awaits a longer run.
4. **`replay --since` is not end-to-end tested** (needs the flag on plus a live
   event DB; refusal path with the flag off verified, exit 2). First real
   replay should be watched.
5. **Bits need a citable positive.** Cross-batch lands work via the 5-turn
   context window; older callbacks stay with persona's CallbackStore.
6. **Pressure list is heuristic.** It catches guilt/dependency/sulking markers,
   not novel phrasing; the judge + owner review are the backstop.
7. Neighbor sanity: `test_personality_lock` + `test_dynamic_personality` +
   `test_persona_tone` — 76 passed, no collateral. Full suite not re-run (2 h;
   my change is additive and unreferenced outside its lane).

## Exact integration steps for CLI 6

1. Read `personality/mind/INTERFACE.md` (5 min).
2. In the reply path, at the start of each turn, build the previous turn's
   `TurnRecord` and call `observe()` — no flag check needed (no-op while off).
3. In `build_persona_block`, after core + NOW: `mind = snapshot(...)`; append
   `mind.text` when not None; add `mind.token_estimate` to the running budget.
4. At process boot: `start_worker()`. Set `EV_PERSONA_MIND_DB` in tests.
5. Do not flip `MIND_ENABLED` until the §13 override is recorded and the brain
   key is funded; then run the sim once with the real brain and check the judge
   verdict before enabling in production.

`MIND_ENABLED` stays False. End of report — CLI 7, EVIE MIND lane.
