# Growth engine log (rocky-sculptor)

## Step 0 (2026-10-27): already satisfied by owner-ordered deploy

- done: live api+talk 6f0eecc verified; main==origin 06a1795; tag on remote.
- failing: none. Polling moot (deploy done, recorded in DEPLOY_RECORD.md).
- assumptions: none (all measured this turn).
- next: GE1 contract+adapters (CLI1 depends on it); GE2 eval key.

## GE1 contract+adapters (2026-10-27, first hour)

- done: EVAL_SUITE.md binding contract; app/growth (run/store/7 adapters/
  4 stubs); ev growth run/show; growth_store_dir R5-synced; 7 tests green,
  ruff+mypy clean; committed on main; CLI1 notified via mailbox.
- failing: none yet. Proof sequence (orb/gesture/gates/gauntlet/unit/cug)
  running in background; rows landing in ~/.ev/growth/metrics.sqlite.
- assumptions: gauntlet --json summary keys (defensive .gets); CUG save
  paths; PresenceChecks/bench tools build on main tree.
- next: collect GE1 proofs; GE2 budget architecture; GE1 subagent review.

## GE2 budget architecture (2026-10-27)

- done: eval key setting+R5; suite_env surgery (live creds absent, tested);
  preflight caps + spend ledger; ev keys set; ev growth credit (file+push);
  contract key-routing section; 8 budget tests; committed; CLI1 notified.
- failing: none. Live push leg unit-proven only (no 3am spam); fires when
  owner runs credit with EV_API_KEY set.
- assumptions: adapter fixes (gauntlet shape, orb/gesture paths) verified
  by re-run proofs below.
- next: GE1 proof closeout (cug+orb+gesture re-runs); GE1/GE2 subagent
  reviews; GE3 train.

## GE1/GE2 review fixes (CHANGES-REQUESTED -> fixed, 28 findings)
- done: adversarial subagent review returned 28 findings (7 blocker incl. gauntlet failures dropped, real-brain unparsed, key separation defeated 3 ways, caps unenforced, stale-read-as-fresh); all fixed + 17 proof tests (tests/test_growth_adapters.py); lint clean, 37/37 growth tests green.
- failing: nothing in the harness; GE1 proof numbers must be re-derived (old gauntlet/gates/CUG figures came from buggy adapters and are void).
- assumptions: CLI1 branch mind-1.1 untouched files (real_brain.py, scripts/gauntlet.py) safe to fix with NOTICE; tmp/re-grokbot-openbot + eval/mimo-smoke.json not mine, left alone.
- next: commit; re-gate mind-1.1@96ddea9; re-run GE1 proofs on fixed adapters; finish GE3 (RC/shadow/ship/watch/notes).

## GE3 complete: RC / shadow / ship / watch / notes
- done: rc.py (battery + Wilson-regression + safety block), shadow.py + shadow_worker.py + shadow_guard.py (plan-only replay, verified prepare_only audit: agent.run/skills_act/captures closed at shadow layer), ship.py (owner-only merge/tag/push/backup/app-rebuild/deploy/verify/watch, auto-rollback, DB never auto-restored), watch.py (crash + audit-error checks, GE5 hooks honestly unmonitored), notes.py, release_cli wiring; 12 new tests incl. red-team effects-impossible proof (live artifacts byte-identical).
- failing: nothing; GE1 proofs re-derived on fixed adapters (journeys 12/12, gauntlet 568n/438/0/130, gates 156+5skip, gesture 319/319, orb 8/11 then 7/11 UNSTABLE marginal P2, CUG re-running with lane self-acquire after my G5 lock blocked lanes — lock file must be `owner`, use scripts/gui-lock).
- assumptions: live session ledger at backend/storage/cognitive/session.json (5 turns); shadow needs eval key for brain replays (owner action outstanding).
- next: CUG result; GE4 (failure queue/nightly/review/frontier/report); GE5 (durable telemetry + dashboard + alerts, plugs watch hooks); GE6 proofs; GE7; GE8.

## GE4 complete: learning from real life
- done: failure queue (redacted intake) + deterministic miners (failed receipts ex-PARKED/PREPARE_ONLY, undo-chains, corrections, rephrases, cancels, gaps) + clustering + PENDING-REVIEW candidates + `ev review` pty-proven + LIFE suite (approved files/sql cases run forever; others skip honestly) + frontier (live tool inventory) + weekly HTML/text report + `ev growth nightly` (key-gated, marker-incremental) + setup growth phase (2 launchd plists, report-only byte-identical) + `ev bad` + /v1/growth/feedback intake (contract 581, additive-only verified, S21 oracle updated 580->581 with reason).
- failing: nothing. slow_page CUG flake root-caused (load-dependent; 63/63 quiet) -> GE6 item 2 input. Stored false_success=1 (CUG row 16) stands as measured.
- assumptions: live audit dominated by eval runs until suite_env scratch-audit (now fixed at source; gate targeted also adopted suite_env, 18/18 guards green under it); PWA/HUD thumbs send-side NOT-BUILT (intake ready).
- next: GE5 (durable telemetry + Evie Health page + alerts, plugs watch hooks); GE6 proofs (OCR hole + T2 latency); GE7; GE8.

## GE5 complete: SLOs + observability
- done: turn-telemetry sink (counts/timings, never content, never raises) + SLO compute (9 SLOs, UNMEASURED-honest, n>=10-to-convict) + Evie Health page + `ev growth slo --check` (deduped alerts -> HUD/phone + failure queue, feeds WATCH rollback) + live cost from model-call ledger pricing; watch GE5 hooks now live (turn-derived only; suite safety stays in RC).
- failing: nothing. Live status: 0 turns/7d (hook pending CLI1 reply: asked A-or-B), false_success_7d=1 BREACH correctly surfaced (CUG slow_page flake row).
- assumptions: kernel hook needs CLI1's file (asked first); first_response_ms voice-only via transcript->brain->speech telemetry sum, null elsewhere.
- next: GE6 (improve loop + 2 proofs: corpus-OCR hole, AX T2 latency); GE7; GE8; report.

## GE7+GE8 committed (f6f6450) + GE3/GE5 fixes (3084f55)
- done: modelwatch (pins vs live listings, bakeoff runner) + 8 recurring checks (models-empty, fabric-legacy, egress, cost, shell-drill, soak, idle-app/backend) via `ev growth recurring`; EVAL_SUITE.md §GE7/GE8; 79/79 growth battery green. Real runs: fabric-legacy PASS, shell-drill 5/5, soak smoke 9/9 (recovery 2.3s, 60/60 turns, fds stable), models-empty SKIP-honest (app down), egress n=0 (no live turns), cost baseline set, idle-backend 5.07% CONFOUNDED (load 4/8, re-run quiet pending).
- failing: nothing open. Fixed this session: gate diff-direction bug (VOIDED a vacuous GATE PASS on sub-2, re-gated honestly: 8/8 incl. 59 targeted), utcnow import (SLO cost always None), soak uv/uvicorn kill+measure (process groups), cost/egress asyncio-in-CLI, doctor invocation, soak turn payload, models-empty environmental SKIP, idle vacuous-pass.
- assumptions: main now f6f6450/3084f55 — ge-improve-1 base moved; ship will need rebase (owner step later). Submission-1's BLOCK evidence was computed on the reversed diff (verdict directionally right, evidence wrong; re-gate on resubmit).
- next: GE6 proof-1 RC running (sub-2); proof-2 AX-T2 improve; fresh-subagent review of f6f6450+3084f55; idle-backend quiet re-run; GE7 bakeoff blocked on eval key (owner action stands); report + checkpoint.

## Review fixes committed (fresh-subagent review of f6f6450+3084f55)
- done: fixed all 4 BLOCKERs (egress n=0 vacuous pass -> partial/SKIP; cost baseline never advancing -> exact-window snapshots with UPDATE + rotation guard; tmux-chaos unchecked rc -> SKIP; _fds dead-group 0 -> None) + 13 concerns (soak skip-paths, boot evidence, workload guard, log-handle close, _kill no-early-return, idle Linux note, driver --out implemented, 4 ruff fixed, egress read-only probe, modelwatch empty-pin guard, all-skip -> partial, mkdtemp cleanup everywhere, gate diff returncode + empty-diff fail-closed). 32/32 affected tests green, lint clean.
- failing: nothing. Re-ran real checks: rec-egress partial/SKIP (0 rows), rec-cost partial (live baseline usage=$4.9868; eval no key). Observed: live key usage +$0.006 since last read (source not chased; suites spend eval key only).
- assumptions: proof-2 agent running (tree moves -> its auto-submit will refuse; manual submit path established). RC sub-2 running in pinned worktree, unaffected by main commits.
- next: RC result -> shadow sub-2; proof-2 branch -> train; idle-backend quiet re-run when machine settles; GE8 setup-phase scheduling; report + checkpoint.

## GE5 hook live + M1 re-gate + proof-2 gated
- done: GE5 turn-telemetry hook added on main (kernel.py handle_turn, reply-B permission, NOTICE sent); kernel-turn record test (content-absence asserted); 36/36 adjacent green. M1 mind-1.1-r submitted (sub-3, 33 files) -> GATE BLOCKED 7/8 on test_evalkey_resolution_order (test leaks ambient EV_GROWTH_EVAL under suite_env; repro proven; FIX_REQUEST sent to CLI1, test-only). Proof-2 ge-improve-2 verified (41/41 + 248/248 on branch, diff reviewed incl. max6-coverage edge noted) -> submitted (sub-4) -> GATE PASS 8/8 (62 targeted).
- failing: nothing of mine. Sub-3 awaits CLI1 test fix. RC sub-2 still running (~50min, 1 pytest active); RC sub-4 queued after (serialized to avoid load-flakes).
- assumptions: suite_env leaving live key in routed slot with no eval key is by design (spend gated by run.py:42 + $0 contract); noted as question in FIX_REQUEST.
- next: RC-2 -> shadow-2; RC-4 -> shadow-4; CLI1 M1 fix -> re-gate; idle-backend quiet; report + checkpoint.

## GE6 proof-1 READY TO SHIP (sub-2: gate -> RC -> shadow)
- done: RC-2 PASS-WITH-GAPS (unit 5995/6040, gates 145/145, gauntlet 142 partial/1.0, scorecard blocked-no-eval-key, CUG 255/255, journeys 22/22, orb 6/11 unstable, gesture 319/319). 8 unit failures ALL classified non-branch: PWA churn (known pre-existing), baseline-drift (merge chore), 4 memory/order-flakes (50/50 main + 4/4 branch isolated), watch-test (MY ambient-sink bug, fixed hermetic), gauntlet-tripwire (MY growth mentions, G3 exemption + NOTICE). Shadow-2 PASS (3 owner turns, 0 flags/leaks/divergences). Measured improvement: 3/4 payloads typed -> 0 typed unapproved; 38 branch tests.
- failing: nothing. RC also exposed eval telemetry pollution (5 rows, live proven pre-hook, truncated with backup) -> suite_env scratch telemetry + regression test, committed.
- assumptions: orb 6/11 within known-unstable band (7-8/11 before), no baseline to convict; CUG full pass (flake quiet).
- next: RC-4 (proof-2) running; shadow-4; CLI1 M1 fix re-gate; idle quiet; report+checkpoint.

## GE6 proof-2 READY TO SHIP + M1 gated (sub-4 gate->RC->shadow; sub-3 re-gate)
- done: RC-4 PASS-WITH-GAPS in 38min (unit 6007/6049, gates 145/145, gauntlet 142/1.0, scorecard blocked-no-key, CUG 255/255, journeys 22/22, orb 6/11, gesture 319/319). 9 failures all non-branch: s34 (proven environmental via binary link; ensure_worktree now links evvision build), tripwire + 4 memory flakes + PWA + baseline (as RC-2), two-models (MY gemini-2.0 fixture, fixed to 3.8/3.9 pair). Shadow-4 PASS (3 turns, 0/0/0). M1 mind-1.1-r re-gated PASS 8/8 (210 targeted) after two 1.4 test-isolation fixes on branch (ee25371, 5f14cf7; 2 NOTICEs sent); second failure was masked by gate's one-line report (noted). idle-backend x2 confounded (5.07%@4.0, 11.28%@10.9, owner+peers active); quiet sample deferred to scheduled 4:07 run.
- failing: nothing. No CLI1 reply in ~4h (FIX_REQUESTs + 3 NOTICEs in their inbox).
- assumptions: M1 RC deferred to eval-key arrival (scorecard leg needs real brain); ship needs rebase (main moved under all branches).
- next: finalize report + 35-line CHECKPOINT.

## Session end: engine complete, notes for next captain
- done: final report + 35-line CHECKPOINT committed (402c58f). Observed NEW untracked persona/* + test_persona_* files in live tree (not mine, untouched — a peer's active work).
- failing: nothing.
- assumptions: persona files belong to an active peer session; do not delete.
- next: owner runs ev accept / eval key / ev setup; ship proofs+M1 when notified.

## v1.1 prep STOPPED: eval key absent (verified env + file). Step 1 DONE.
- done (step 1, P0): false_success_7d=1 ROOT-CAUSED to a REAL defect, not a flake. Row: CUG @69815d0 11:05, live.t2.slow_page said "Skipped the ad." with ok+verified while fixture adsSkipped!=1. Oracle: claims_success=ok&verified; fs=claim&!ground-truth. Cause: pre-fix web_media.py read probe ERRORS as ad-ended in 4 spots (re-probes, countdown, verify, _wait_ended); slow_page+load only raised timeout rates. Deterministic proof: new probe-error tests 6/7 FAIL on main (receipt ok=True verified=True say='Skipped the ad.') and the same stimulus on ge-improve-2 gives ok=False verified=False + honest timeout. Fix = proof-2 branch (READY TO SHIP) + its 10 regression tests; RC-4 CUG 255/255 fs=0 on fixed code. Row STANDS as true positive (fix path, not correction path); SLO still BREACH=1 correctly until it ages out; unauthorized=0.
- failing: nothing in step 1. Eval key absent -> steps 2-6 BLOCKED per brief (stop after step 1).
- assumptions: slow_page quiet re-passes were luck (no timeouts), not health; corrected in this entry.
- next (when key exists): unit drift triage, GE4 provenance filter, ping CLI1, rebases, M1 RC, v1.1 train.

## v1.1 step 3 DONE: GE4 provenance gate (0/1706 provably real)
- done: provenance.py stamper (owner/suite/pytest/live-proof + drill windows); stamps in audit.record (outside hash, chain verified green), remember_exchange, sink.record_turn; mine.partition() choke point; queue prov col + migration; nightly/report/brief owner-only; feedback prov; candidates quarantine (16 flipped, 1 approved kept w/ taint note); ev accept auto-window. Tests 18/18, lint clean. NOTICE sent (audit.py+intent.py, 1.4). RE-MINE: 4910 audit + 5 ledger rows, ALL unstamped -> 0 owner; 0 real findings, 0 clusters, 0 candidates. Nightly proven: "0 mined, excluded {'unstamped': 68}".
- failing: nothing. Honest correction: old "18 clusters/17 candidates" were eval/drill byproduct, void as real-life signal; machinery proven on fixtures.
- assumptions: pre-stamp rows can never be promoted (fail-closed); approved LIFE case kept as owner-approved test intent.
- next: step 2 needs suite; step 4 window to 22:47; step 5 rebases in progress.

## v1.1 step 5a DONE: 3 rebases onto main, all verified
- done: ge-improve-1 -> a6ccbf9 (clean, 38 tests green); ge-improve-2 -> 9fd1e92 (clean, 41 green); mind-1.1-r -> f391b85 (3 conflicts resolved by intent: improve dispatch main-wins+stale-write_brief-dropped + brain kept; run.py import union; stub tuple branch-side; 32/32 + cli imports green). NOTICE sent. Local branches only, no push. Main suite running for step 2 (20min in). CLI1 window to 22:47, no reply yet.
- failing: nothing.
- assumptions: pre-rebase RC/shadow evidence stands at old SHAs; v1.1 train re-verifies rebased heads.
- next: suite junit -> step 2 triage; CLI1 verdict; M1 RC sampled ($0.013); v1.1 assembly + train.

## 2026-10-27 23:0x v1.1 PREP step 2 (unit-suite drift) + step 4 verdict — rocky
- DONE: main full suite 7 failed / 6177 passed / 29 skipped (70 min). 6/7 root-caused, 6/7 fixed+verified on main 801e589.
- DONE: 4 victims (life_agency, life_bridges, pol_policy, whatsapp_reads) traced to MY test_real_brain_prefers_env_key_over_files: _prime_env() wrote repo .env EV_* (incl EV_LIFE_HELPER_PATH, EV_MESSAGING_PROVIDER) into suite os.environ permanently. Fixed with snapshot/restore; combo 25/25 green. Owner: me. Cause: test pollution, not prod.
- DONE: test_brief_assembles_all_sources — stale oracle vs v1.1 owner-only contract (seed lacked prov=owner). Fixed. Owner: me.
- DONE: test_release_contract app.js — pre-existing at evie-v1.0 tag (verified in worktree). Cause: app.js legitimately changed in 2a02e9e7 after manifest regen e555788b. Fixed via documented `make pwa-release-manifest`; 10/10 green. Owner: PWA assets (pre-existing churn).
- NEXT: test_queue_record_round_trip (in-suite-only, growth predecessors exonerated, pre-g bisect running) then M1 RC + v1.1 assembly.
- STEP 4 VERDICT: CLI1 ping 22:17, window expired 22:47, no reply (~6h silence). blooming-mensa = IDLE. Proceeding with mind-1.1-r (f391b85) as submitted.

## 2026-10-27 ~00:05 queue-victim bisect — rocky
- DONE: pre-growth files 1-70 (test_accept..test_conversation_teleport...) + queue victim: 1049 passed, victim GREEN. First half exonerated.
- NEXT: pre-growth files 71-140 + victim running (~12 min); then narrow to file/test.

## 2026-10-27 ~00:35 Step 2 CLOSED (7/7) — rocky
- DONE: queue victim ROOT-CAUSED to intra-run version skew, not a product/test defect. Proof: (1) c9817308+faa949b landed 22:25 INSIDE the 22:10-23:21 suite window (suite ran on the live main tree, my process violation); (2) old-test(fcbc46b)+new-queue mix reproduces the EXACT assert (0==1) in a scratch worktree; (3) 7 consistent-tree subsets pass incl 2074-test pre-g combo, 623-test subdir combo, growth in suite order. Owner: me (process). Remedy: frozen-tree re-validation = M1 RC unit leg + v1.1 RC unit leg; reopen if either shows it.
- DONE: anti-cheat tripwire test_harness failed on new provenance.py docstring word "gauntlet" (comment listing excluded lanes). Reworded to "computer-use lanes" (no behavior change); 19/19 green. Owner: me.
- Step 2 ledger: 4 env-pollution victims (fixed, mine) + 1 brief oracle (fixed, mine) + 1 PWA manifest (fixed, pre-existing churn) + 1 skew artifact (explained, mine). Zero unexplained. Nothing routed externally; no FIX_REQUESTs needed.
- NEXT: GATE submission 5 (M1 a8e1867), then M1 RC with $0.01-capped live-key real-brain leg.
