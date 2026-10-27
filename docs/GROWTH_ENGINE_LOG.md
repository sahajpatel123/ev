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
