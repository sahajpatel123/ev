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
