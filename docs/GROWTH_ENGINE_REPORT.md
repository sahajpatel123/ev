# Growth Engine Report (rocky-sculptor)

Live release: `evie-v1.0` (`abd7107`, on remote) running commit `6f0eecc54e`
(api :8000 + talk :18000); main at `2ada9cc` (+hook/scheduling commits).
Brain: Muse Spark via OpenRouter (live), key separation enforced (GE2).
Verdict on the machine: WORKING, with two proofs in the train and one
owner key outstanding. Details + CHECKPOINT at the end.

## GE1 — Eval suite contract: WORKING (proven)
- Contract: `docs/contracts/EVAL_SUITE.md` (+GE7/GE8 section). Standard
  result v1: Wilson95 over DECIDED cases, skips disclosed never scored.
- Adapters for unit/gates/gauntlet/cug/scorecard/journeys/orb/gesture/
  latency/soak/life + 8 recurring checks + modelwatch. Store:
  `~/.ev/growth/metrics.sqlite` (append-only, keyed by commit+date).
- Re-derived proofs on fixed adapters: journeys 12/12; gauntlet 568n /
  438 pass / 0 fail / 130 held-out skip; gates 156 pass + 5 skip;
  gesture 319/319 (12005 events, 0 activations); CUG 254/255 quiet 63/63
  (slow_page = load flake, now GE6 proof-2's target); LIFE 1/1 approved.
  Orb 7-8/11 UNSTABLE marginal (CPU 2.05-2.39% vs 2.0% + GPU spikes, P2).

## GE2 — Budget architecture: WORKING (proven)
- Eval key routing (`ev keys set openrouter-eval`), suite_env strips live
  creds + dummy vault + scratch audit + NO_SECRETS_FILE. Caps $1/day,
  $20/month clamped; no-eval-key + budget>0 -> blocked (run.py:42).
- Credit watch proven (`blocked` without eval key). OWNER-ACTION stands:
  create eval key (~$20/mo), cap live (~$10/mo). Live key usage observed
  $4.9811 -> $4.9868 (+$0.006, source not chased; suites spend eval only).

## GE3 — Release train: WORKING (proven, one bug found+fixed by use)
- submit/gate/rc/shadow/ship/watch/notes wired (`ev release *`).
- GATE BUG FOUND BY PROOF-1: swapped diff args vacuously passed
  ahead-branches; voided one PASS, fixed, regression-tested, re-gated
  honestly. Gate now fail-closed (bad ref + empty diff).
- Shadow: prepare_only + guard proven by red-team (live artifacts
  byte-identical). Ship: owner-only TTY + backups + auto-rollback
  (DB never auto-restored). Watch: crash/audit/live-SLO legs.
- mind-1.1-r (M1, sub-3): GATE 7/8 BLOCKED on one CLI1 test bug
  (ambient EV_GROWTH_EVAL leak; FIX_REQUEST sent, test-only).

## GE4 — Learning from real life: WORKING (proven)
- Failure queue + miners (PARKED/PREPARE_ONLY excluded) + clustering +
  candidates + `ev review` (pty-proven) + LIFE suite + frontier +
  weekly report + `ev growth nightly` (key-gated, marker-incremental).
- Nightly proven: 1706 failures mined (1312 failed-receipt, 245 denied,
  148 undo, 1 failed-turn), 18 clusters, 17 candidates.

## GE5 — SLOs + observability: WORKING (telemetry just unblocked)
- 9 SLOs, UNMEASURED-honest, n>=10 to convict; Evie Health page;
  `ev growth slo --check` dedupes alerts to HUD/phone + failure queue.
- FIXED: utcnow import (cost always None) + regression test.
- Turn-telemetry hook ADDED ON MAIN with CLI1 reply-B permission
  (NOTICE sent): handle_turn records counts-only rows; content-absence
  test green. Live SLOs measurable after the next ship (hook on main,
  live runs pre-hook 6f0eecc; 0 turns/7d before).
- false_success_7d=1 BREACH correctly surfaced (CUG flake row stands).

## GE6 — Self-improvement loop: PROOF-1 IN RC, PROOF-2 GATED (pending RC)
- Machinery: P2 queue + briefs + headless `muse exec` + budget + snapshot
  guard (refused submit twice, correctly) + branch+tests discipline.
- Proof-1 (P2 #7 corpus-OCR hole): branch ge-improve-1 @6d0b8d9 —
  screen (metachars+injection+shell-policy+allowlist), ground vs local
  stores, approval threading. Before: 3/4 typed cmds carried payloads.
  After: 0 typed unapproved; 38 branch tests pass. GATE PASS 8/8
  (59 targeted). RC RUNNING (sub-2). [shadow + measured delta: PENDING]
- Proof-2 (P2 #1 AX T2 latency): branch ge-improve-2 @0eeba3d —
  one AX walk/poll + button-first, DOM-interleaved polling, probe
  errors fail honestly (false-success mechanism closed). Measured
  (virtual clock): AX+load p95 11.0s->5.0s; walks halved; DOM
  byte-identical. 41/41 + 248/248 verified. GATE PASS 8/8 (62
  targeted). RC QUEUED after RC-2. [RC + shadow: PENDING]

## GE7 — Model watch: WORKING (bake-off runner ready, live run needs key)
- Weekly pins-vs-listings proven (spark 6 seen, gemini-live 9
  bidi-capable, pins listed, newer=none). Bake-off runner ready
  (stratified scorecard+LIFE+gauntlet, manual recommendation only).
  Real bake-off BLOCKED on eval key (owner action).

## GE8 — Recurring checks: WORKING (proven, scheduled, 1 re-run left)
- 8 checks, all real runs in store: fabric-legacy PASS; shell-drill 5/5;
  soak smoke 9/9 (recovery 2.3s, 60/60 turns, fds stable, no leak);
  models-empty SKIP-honest (app down, socket-proven); egress SKIP
  (0 rows, counter pending CLI1); cost baseline set (exact-window);
  idle-app SKIP (no app); idle-backend 5.07% CONFOUNDED (load 4/8).
- Scheduled via setup growth phase: recurring daily 4:07, soak Sun 2:07,
  modelwatch Mon 5:07 (report-only byte-identical; activation = owner
  runs `ev setup`). [idle-backend quiet re-run: PENDING]

## Review + honesty ledger
- Fresh-subagent review of GE7/GE8: 4 blockers + 13 concerns, ALL FIXED
  (vacuous passes closed, baseline advancing, skip-paths, hygiene,
  lint clean), 32/32 tests, committed.
- Voids on record: 1 vacuous GATE PASS (sub-2, re-gated); sub-1 BLOCK
  evidence superseded (reversed diff); early rec-* rows from buggy
  check versions (superseded by fixed-code runs, noted here).

## Owner actions (unchanged + two)
1. `ev accept` (once, now) — 8 guided checks remain.
2. Create eval key + caps (unblocks scorecard/FRESH/bake-offs/shadow
   brain replays): `ev keys set openrouter-eval`, ~$20/mo; cap live ~$10/mo.
3. Run `ev setup` (activates nightly/recurring/soak/modelwatch schedules).
4. `ev review` weekly (a few minutes). `ev release ship` when notified.

## CHECKPOINT (max 35 lines)
(PENDING RC-2/shadow-2, RC-4/shadow-4, idle quiet re-run — then final.)
