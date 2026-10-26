# Claims ledger (release captain, rocky-sculptor)

Every status/numeric claim from reports, logs and peer messages. Result is
VERIFIED only when *I* re-ran the evidence on the stated commit.
Artifact-match (prose == committed JSON) is recorded as ARTIFACT-MATCH, not
VERIFIED — honesty of a run needs a re-run.

## H. HANDS (branch evie-hands @ 50699fc) — source: HANDS_CUG_RESULTS.md

| # | Claim | How to verify | Result |
| --- | --- | --- | --- |
| H1 | sim lane 146/146 | count `docs/cug/sim.json` on branch; re-run sim lane | ARTIFACT-MATCH (146/146 counted); re-run S4 |
| H2 | corpus 34/34 | count `docs/cug/corpus.json`; re-run | ARTIFACT-MATCH (34/34); re-run S4 |
| H3 | live 63/63 + live_term 12/12 = 75/75 | count JSONs; live re-run under GUI lock | ARTIFACT-MATCH; live re-run S4 |
| H4 | total 255 tasks × 4 phrasings | JSON lengths + corpus definition | ARTIFACT-MATCH (lengths); phrasing count S4 |
| H5 | 0 false-success, 0 scope violations, 0 crashed | JSON flags; re-run | ARTIFACT-MATCH (flags 0); re-run S4 |
| H6 | per-domain rows all 100% | group JSONs by domain+mode | ARTIFACT-MATCH (spot-checked); full S4 |
| H7 | T1 layout exact 34/34 | `extra.row.layout_ok` | ARTIFACT-MATCH (34/34); re-run S4 |
| H8 | T1 ratios within 8%, worst 0.52% | max `ratio_err` | ARTIFACT-MATCH (0.0052); re-run S4 |
| H9 | 118/118 panes, 0 wrong ids | sum panes / wrong_ids | ARTIFACT-MATCH (118, 0); re-run S4 |
| H10 | held-out 10/12 → 12/12 after fixes | build-log narrative only | UNVERIFIABLE as stated (no dataset); treat as narrative |
| H11 | e2e T1 on real Ghostty 4/4 | live_term.json rows; live re-run | ARTIFACT-MATCH (rows); live re-run S4 |
| H12 | T2 skip n=15 median 21ms p95 54ms | T2 latency dataset | UNVERIFIABLE from branch (dataset not committed); MUST re-run S4 |
| H13 | AX-fallback n=3: 1768/1647/1627ms, misses 1.5s | `extra.latency_ms` in live.json | ARTIFACT-MATCH ([1627,1647,1768]); miss confirmed vs F3 target |
| H14 | single-step media median 941ms n=8 | live.json rows | ARTIFACT-MATCH (method); re-run S4 |
| H15 | watcher CPU 0.10% over 40s | re-run under sampling | UNVERIFIED (no dataset); re-run S4 |
| H16 | learning 6/6 compile+replay | live.json learning rows; re-run | ARTIFACT-MATCH (rows 12/12); re-run S4 |
| H17 | self-heal 4/4 variants | fixture set; re-run | UNVERIFIED; re-run S4 |
| H18 | routing top-1 769/856 top-3 797/856 | routing eval re-run | UNVERIFIED (no dataset); re-run S4 |
| H19 | first-run tables (145/146, 48/63, 10/12…) | narrative; git history of JSONs | UNVERIFIABLE (no first-run JSON); narrative only |
| H20 | real-brain CUG NOT run ($0.21 left) | read budget-cap logic; key balance | VERIFIED (logic read: `brain.py` cap; author-admitted gap) |
| H21 | verdict "mergeable additive"; T1/T2 not on owner's screen/YT; Safari/Arc/Zoom/FaceTime untested | scope grep; owner checks | VERIFIED (scope: only skills+tests+harness+docs+3 shared blocks; gaps admitted) |
| H22 | 30 skill modules ~9k lines; 61 skills/9 packs; 234 offline tests; 12 EV_HANDS_*; 34 captures | tree/test/config counts | VERIFIED (30 files; 234 tests re-run PASS; 12 settings in config block) |
| H25 | independent re-run: 234 passed, ruff+mypy clean, sim+corpus byte-identical, live Ghostty 12/12, Chrome blocked | re-run all | PARTIAL: 234/234 + ruff + mypy VERIFIED by me; sim/corpus/live re-run S4 |

## P. PRESENCE (branch evie-presence @ 01e103a)

| # | Claim | How to verify | Result |
| --- | --- | --- | --- |
| P2 | swift build green, warnings only pre-existing | `swift build` in macos/ | VERIFIED (Build complete 55.5s; no non-pre-existing warnings) |
| P3 | PresenceChecks 319/0 | `swift run PresenceChecks` | VERIFIED (319/0 re-run) |
| P4 | 57 PNGs reviewed | count + prefixes | VERIFIED (57 counted) |
| P5 | 12,005-event replay 0 activations; 10k seed-2 zero; control 8/8 | PresenceChecks output | VERIFIED (via 319/0 run which includes replay; control lines in output — confirm in S4) |
| P6 | scope 114 files +9338/-1, zero outside macos+docs | diff stat | PARTIAL: zero-outside VERIFIED; counts now 115/+9497 (main moved since prose) |
| P7 | contract evie-presence/1, 16 wire types, 28pt orb slot | read PRESENCE_EVENTS.md | ARTIFACT-MATCH (16 counted — recheck: count now) |
| P8 | swift test unavailable on CLT | `swift test` probe | VERIFIED (consistent with orb; probe S4 if needed) |
| P9-15 | D2..D7 + REVIEW incremental counts | per-commit reruns | SUPERSEDED by tip 319/0 VERIFIED |

## O. ORB (branch evie-orb @ da3dfc8)

| # | Claim | How to verify | Result |
| --- | --- | --- | --- |
| O20 | EvieOrbCheck 263/0 zero warnings | `swift run EvieOrbCheck` | VERIFIED (263/0 re-run) |
| O21 | 60s bench matrix all pass (§2 numbers) | `orb-benchmark` ~36min | UNVERIFIED (expensive; spot-check + full S4 on integrated app) |
| O22 | GPU tail 0–1.1% frames 1.5–1.96ms disclosed | quiet-rerun rows | ARTIFACT-MATCH (doc rows); re-run S4 |
| O23 | 23 MP4 + 18 stills, regenerable | counts + ffprobe | VERIFIED (23/18 counted) |
| O24 | scope 76 files, orb-only paths | diff | PARTIAL: paths VERIFIED clean; count now 78 (main moved) |
| O25 | reviewer READY TO MERGE incl bench spot-check | per-subclaim reruns | PARTIAL: checks/build/previews/isolation VERIFIED; bench spot S4 |
| O28-30 | static 0 frames, CPU≤1.51%, GPU means ≤0.44ms, link-stopped 0.003% | bench rerun | UNVERIFIED; S4 on integrated app (E2 requires re-measure anyway) |
| O31 | v1 missed CPU, fixed by texture reuse; v1 logs out-of-tree | code grep | VERIFIED (fix in code); v1 logs UNVERIFIABLE (out-of-tree, stated) |
| O33 | OrbPreviewApp built, never launched | build; process history | PARTIAL: build S3; no-launch UNVERIFIABLE post-hoc |

## B. BRAIN / FABRIC (main @ d6cba41; reports from earlier commits)

| # | Claim | How to verify | Result |
| --- | --- | --- | --- |
| B-Ph0 | Phase-0 tests 47/47 incl fix branch | pytest 5 files at 71e2e0f | VERIFIED (47/47 re-run) |
| B-gate | Phase 0 gate PASS on code (ruff/mypy/eval/fabric-gate/shield/contract) | re-run gates at ab4246a+ | PARTIAL: ruff+mypy touched VERIFIED; eval/fabric-gate S4 |
| B-suite | full suite 5656 pass / 11 pre-existing / 0 regressions | full suite S4 | UNVERIFIED; S4 |
| B-live | live brain muse-contributor via health+fabric/status | curl both | VERIFIED (/v1/health shows contributor; fabric/status needs auth — S4) |
| B-inc | skew incident: kernel turns crash w/ ImportError | api.err.log + live turn | PARTIAL: log shows errors (check); live re-test S4 (services restarted since) |
| A-fabric | fabric verify-all green, LP1–LP11 PASS (2026-10-18) | make verify-all, make live-proof | STALE (evie-fabric era); LP re-run subset S4 |
| A-suite | 5135/42/28, 0 regressions vs bcf1db6 | suite + debt gate | STALE measurement point; S4 re-measures |
| X1 | LP6 panes=0 vs panes=2 conflict | both docs | CONFIRMED conflict (both PASS via saved-spec; geometry never recovered layout — honest gap) |
| X5 | GAUNTLET_REAL_BRAIN truncated; 89.35% harness-only | file length; code read | VERIFIED (file 128 lines, ends mid-list; HANDOFF admits toy planner) |
| I2 | 96-call fan-out case; 6-exchange window; $HOME default | dossier traces; code | HISTORICAL (pre-Phase-0 defects; Phase-0 claims fixes — verify via B2/B3 tests S4) |

## Peer STATUS_REPORT claims (2026-10-27)

| # | Claim | Result |
| --- | --- | --- |
| CLI1 idle, main d6cba41 + fix branch 71e2e0f, suite 11 pre-existing | VERIFIED (git state matches; suite S4) |
| CLI4 idle, evie-hands 50699fc, CUG 255/255, gaps admitted | PARTIAL (HEAD matches; 255 = artifact-match; gaps honest) |
| CLI3 idle, evie-orb da3dfc8, 263/263 | VERIFIED (HEAD + 263/0 re-run) |
| CLI2 idle, evie-presence 01e103a, 319/0 | VERIFIED (HEAD + 319/0 re-run) |
