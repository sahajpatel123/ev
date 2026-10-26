# BRAIN DOSSIER (paste version) — Evie intelligence recon, 2026-10-26

Self-contained planner brief. 44 probe turns (A=live-settings kernel ×21,
B=same kernel + Muse Spark Contributor ×8, C=raw Muse Spark ×15), $0.23
spent, isolated instance only. No secrets.

## 1. HEADLINE — why Evie feels unintelligent

1. No working memory of the conversation (5/5 recall probes failed).
2. Tool work disappears into background with no delivery (6/16 turns).
3. One turn fired 96 identical brain calls; spend is unmeasurable inside Evie.
4. Live brain is DeepSeek at effort `low` for chat — not Muse Spark.
5. "Workspace" defaults to the entire home directory.
6. Brain sees only the last 6 exchanges (<1% of the 1M window).
7. Raw Muse at effort high passes all equivalent tasks — the harness is
   the gap. At medium it spins or acts destructively (deleted a file once).
8. Swapping in the contributor brain fixes ambiguity but adds flakiness.
9. Doctrine gags the brain from mentioning tools; responses hide all traces.
10. Durable goals DO persist (a task survived a server restart) — but there
    is no plan/verify/reflect discipline anywhere.

## 2. WHAT LIVE RUNS

`:8000` = commit `5cf2947`, mode `realtime_delegate`, role `legacy`.
Brain: `deepseek/deepseek-v4.1-flash` via OpenRouter (effort medium work /
low chat). Voice: Gemini Live `gemini-3.8-live-extended-thinking`.
Working-tree `.env` already says `muse`/`spark` — live predates that cut.

## 3. ARCHITECTURE

`POST /v1/cognitive/turn` → `arun_kernel_turn` → `_brain_turn`
(compile prompt, 12 or 44 tools) → OpenRouter chat w/ native tools →
serial tool loop → 13 policy-gated fabric lanes → spoken + kind only.
Side exits: 24-step `agent.run` loop; `goal.ensure` background delegation
(results never reach the text channel). Other channels' chains: UNKNOWN
(only this path was traced).

## 4. BRAIN CALL + PROMPT

Temp 0.7 (0.2 agent/structured), native tools + auto choice, serial
execution, fail-closed text fallback, no `reasoning.effort` on live code,
timeout = remaining turn budget, no retries seen. Prompt =
`cognitive/context.py::compile_context` (~1,200 base tokens: persona gag
rule, CORE/MEMORY/POLICY doctrine, lane directory, clock, intent ≤2000ch,
work status) + conditionals (6-exchange window, ≤6 memory rows, domain
directives, work-shape, policy). Totals: greeting ~3.5k tok (0.3%),
file task ~9.7k (0.9%). 30 turns of history still shows 6.

## 5. TOOL SURFACE

44 semantic tools (not 31), 5,873 schema tokens; compact turns get 12.
Six overlapping file/compute routes (`files.act`, `digital.act`,
`code.act`, `computer.perform_effect`, `agent.run`, `shell.run`) split by
prose only. Results are structured receipts (`ok/verified/evidence/
spoken`) — good at lane level, but the brain keeps only the last 4
(≤1200 chars) and the API returns none of it.

## 6. THE LOOP

Kernel: 25s chat / 90s work, 4/12 tool-turn caps, hard 16-iteration
ceiling; timeout → `unavailable` or background. Agent: 24 steps (cap 64),
privacy gate, approval gate. Goals persist across restarts
(`steering_version`, patch/cancel). Missing: plan, verify, reflect,
recover prompts; `replans` never fired.

## 7. PREEMPTION (heuristic overrides; fires in 44 probes)

Egress/device gates (1 each), turn-budget backgrounding (6+), compact-path
collapse to 12 tools + low effort (5+), `$HOME` default root (all early
turns), domain hard-directives + app-task classifier (in-code, uncounted),
reflexes/privacy-gate/cost-cap (0 each).

## 8. MEMORY

Thread history (limit 50, filtered) → prompt keeps last 6 exchanges +
6×280ch memory rows + ≤4 evidences. No compaction. Growth flat after 6
turns at every scale (20/100/500 identical). Recall fails because the
greeting falls out of the window and retrieval misses.

## 9. PROBE RESULTS

Pass rates — A 8/21 (38%), B 5/8 (62%), C-med 9/11 (82%), C-high 4/4.
HARNESS PENALTY (C − best(A,B)): reasoning 0, chains +100 vs B, ambiguity
+50/+100, recovery +50, memory N/A-vs-broken, opinion 0, novel +100
(foreground). Failures: harness-limit 9, ignored-context 5, rest 1 each.
Memory fails 4/4 systematically in A; C-medium ambiguity is flaky
(ask/pass, ask/pass, destructive-delete, 12-step spin across 4 runs).

## 10. TARGET-BRAIN READINESS

Settings-only swap PROVEN on live code: `EV_CHAT_PROVIDER=openrouter` +
`EV_OPENROUTER_MODEL=meta/muse-spark-1.3-contributor` (native tools,
effort = default medium, no effort control). Both ids verified on
OpenRouter 2026-10-26: 1M ctx, tools + mandatory reasoning (default
medium), $0.10/$0.20 vs $1.25/$4.25 per 1M. Still needed: deploy HEAD's
`muse` provider (effort param), cache pinning + `cached_tokens` logging,
ZDR routing + Meta retention check, `usage.cost` plumbing.

## 11. HYPOTHESIS VERDICTS

H1 brain≠target CONFIRMED / path≠kernel REFUTED (real kernel reached).
H2 throttle CONFIRMED (effort, budgets, window, fan-out; not schema size).
H3 heuristics CONFIRMED. H4 thin results SPLIT (lanes good, assembly bad).
H5 no-discipline CONFIRMED / no-persistence REFUTED. H6 weak memory
CONFIRMED. H7 doctrine PARTLY CONFIRMED.

## 12. TOP 15 BLOCKERS (file + one-line fix)

1. 6-turn window (`context.py`, `intent.py`) — widen + reference-rank.
2. `in_flight` sink (`kernel.py`, `api/cognitive.py`) — deliver results.
3. 96-call fan-out (`agent/loop.py`, `kernel.py`) — cap + dedupe.
4. `$HOME` root (`agent/tools.py:92`) — require explicit roots.
5. Dropped usage (`kernel.py:as_dict`) — attach usage+cost everywhere.
6. Low chat effort (`speed.py`) — default medium.
7. Device dead-end (`ev/policy.py:683`) — pairing path.
8. Context bleed (`context.py`) — mark resolved turns.
9. No turn trace (`api/cognitive.py`) — return tool summary.
10. Domain directives (`context.py:185-215`) — soften.
11. Tool gag rule (`context.py:98`) — allow on-ask mentions.
12. Thin memory block (`context.py:243-250`) — raise + cite.
13. 25s timeout (`kernel.py`) — scale by work.
14. Missing fire counters — add telemetry per gate.
15. No effort knob on B (`cloud_providers.py`) — add it.

## 13. KEEP / REWRITE / DELETE

KEEP: prompt-block structure, receipt envelope, goal durability, registry
seam, privacy boundary. REWRITE: history+retrieval, delegation delivery,
fan-out, usage plumbing, effort policy. DELETE candidates: `mimo` naming,
text-fallback (after telemetry), UNEXERCISED legacy paths (audit first).

## 14. OPEN QUESTIONS (defaults)

1. +$5 key credit for the full matrix? Default YES.
2. Restrict roots (fail closed)? Default YES.
3. Chat effort → medium? Default YES (text), low for voice-first-word.
4. Background results: poll endpoint? Default YES.
5. Rename `mimo_*` → `brain_*`? Default YES + alias.

## 15. APPENDIX (full repo)

`docs/BRAIN_DOSSIER.md`, `docs/brain-probe/PROBES.md`, `prompt-fragments.md`,
`tool-schemas.json`, `traces/` (44), `live-path-map.json`,
`docs/PROVIDER_FACTS.md`.
