# Traceability (release captain, rocky-sculptor)

Status per §2 master requirement. VERIFIED only from my own re-runs.
Owners: B=blooming-mensa, P=terra-orbit, O=coastal-thalassa, H=light-setebos, RC=me.

## A. Foundation and safety

| ID | Requirement | Owner | Status | Evidence / next |
| --- | --- | --- | --- | --- |
| A1 | Policy engine single authority; audit hash chain verified | B | PENDING | fabric gate + audit test S4 |
| A2 | Receipts on every effect; claims only from verified receipts | B/H | PENDING | S4 (incl voice claim monitor) |
| A3 | Untrusted never instructs; injection corpus 100% blocked | B | PENDING | injection corpus run S4 |
| A4 | Files: explicit roots, snapshots/undo, Trash, CONFIRM, DENY | B | PENDING | LP1 re-run + root tests S4 |
| A5 | Shell sessions; SQL lane; comms draft→confirm→send→verify; grants off | B | PENDING | LP2/LP7 + comms tests S4 |
| A6 | `ev panic` ≤2s; EV_FABRIC=0 legacy works | B | PENDING | S24/S29 tests + live panic S4 |
| A7 | 550 paths byte-identical; additions recorded; settings drift clean | RC | PARTIAL | no branch touches contract/pyproject (VERIFIED); drift test S4 |
| A8 | egress ledger 100%; capture exclusions; hard deletes; secrets scan clean | B/H | PARTIAL | secrets scan clean on 4 branch diffs (VERIFIED); ledger/exclusions/deletes S4 |
| A9 | two-model only; no local weights; doctor passes models-dir empty; no double speaks; models verify | B | PARTIAL | no ML imports in new code; OCR=Apple Vision helper (VERIFIED); doctor/models-verify S4 |
| A10 | `ev keys`; cost ledger ±5%; caps + honest degradation | B | PENDING | S4; key credit ~$0.21 binding |

## B. Brain

| ID | Requirement | Owner | Status | Evidence / next |
| --- | --- | --- | --- | --- |
| B1 | live runs merged commit, Muse brain; effort policy | B | PARTIAL | /v1/health: d6cba41 + contributor (VERIFIED); live trace S4 |
| B2 | one loop: no silent turns, fan-out cap+96-regression, plan/verify/recovery/budgets/delegate/steer | B | PARTIAL | 47/47 Phase-0 tests VERIFIED; full B2 S4 |
| B3 | living thread; ledger compaction; notebook; recall; caching measured | B | PENDING | S4 (mostly NOT-BUILT in Phase 0 — confirm) |
| B4 | ~14-18 tools wired to SKILLS_API; structured results + read_result | B/H | PENDING | S3 seam + S4 |
| B5 | doctrine behaviors | B | PENDING | S4 |
| B6 | turn traces + `ev trace`; brain_* telemetry | B | PENDING | S4 |
| B7 | scorecard ≥120 probes; penalty ≤0; memory ≥90%; n + 95% CI | B | PENDING | S4 if budget allows; else OWNER-ACTION + sampled |
| B8 | gauntlet ≥90% mutant, ≥35% hard, DEV/FRESH real-brain, 0/0 | B | PENDING | S4 if budget allows; note X5 (89.35% was harness-only) |
| B9 | all channels reach One Mind + same thread | B | PENDING | route traces S4 |

## C. Voice

| C1 | Gemini Live hears/speaks only; non-blocking turns; reconnect; mic gating | B | PENDING | S4 |
| C2 | barge-in ≤300ms p95, cancel ≤2s, no orphans; claim monitor ≥95%; spoken yes never approves | B | PENDING | S4 |
| C3 | TTFA/first-token p50/p95 measured; 1500ms budget truthful | B | PENDING | S4 (prior: 3945/12358ms — over budget, honestly reported) |

## D. Presence — P: P2/P3/P4 VERIFIED pre-merge; rest S4

| D1 | gesture + 0 activations/≥10k replay; secure-input; setting-gated | P | PARTIAL | 319/0 incl replay VERIFIED; calibration/owner-run S4/S7 |
| D2 | HUD states/plan/looking-at; no focus steal | P | PARTIAL | snapshots VERIFIED; live HUD S4 |
| D3 | confirm card; approvals API; double-approve/stale | P | PARTIAL | model+snapshot checks VERIFIED; live approvals S4 |
| D4 | rewind timeline + undo | P | PARTIAL | checks VERIFIED; live S4 |
| D5 | server-side presence stream per contract | P/B | NOT-BUILT | S3 seam (contract exists client-side; server emits nothing yet) |

## E. Orb — O: checks VERIFIED; perf/live S4

| E1 | shader/states/audio-reactive/a11y | O | PARTIAL | 263/0 VERIFIED; audio-source wiring S3 |
| E2 | 0 frames idle; ≤2% CPU; ≤1.5ms GPU @120pt | O | PENDING | re-measure on integrated app S4 |
| E3 | island placement/expand/focus/fullscreen | O | PENDING | integrated app S4 |

## F. HANDS — H: offline gates VERIFIED; live/replay S4

| F1 | SKILLS_API wired to brain tools; world model queries | H/B | PARTIAL | 234/234 + registry VERIFIED; brain wiring S3 seam |
| F2 | T1 ≥90% exact, ratios ±8%, ids exact-or-flagged, confirm, read-back | H | PARTIAL | artifacts match (34/34, 0.52%, 118); re-run + J3 S4 |
| F3 | T2 ≤1.5s p95; honest unskip/lock/asleep; phone channel; watcher | H | PARTIAL | n=15 dataset MISSING (UNVERIFIABLE); AX-fallback 1.6-1.8s misses target (admitted); re-run S4 |
| F4 | CUG ≥150 tasks: ≥90% fixtures, ≥85% live, 0/0 | H | PARTIAL | 255/255 artifact-match; sim+corpus re-run + live subset S4 |
| F5 | learned ≥80%, self-heal ≥70%, never from untrusted | H | PENDING | re-run S4 (no committed episode logs) |
| F6 | watchers idle ~0, ≤1% polling, low-risk only | H | PENDING | re-run S4 |

## G. System/ops

| G1 | Evie.app builds once, signs, TCC, login item, bridge | RC | PENDING | S3 build |
| G2 | all `ev` subcommands behave | B/H/RC | PENDING | S4 (+ `ev accept` S7) |
| G3 | iPhone PWA/Shell/approvals/stop/status/push/Shortcuts; Tailscale Serve on/Funnel off | B | PENDING | S4; cellular OWNER-ACTION |
| G4 | suite: no new failures vs baseline; eval green except listed skips | RC | PENDING | S4 |
| G5 | 2h soak + chaos; idle CPU app ≤1% backend ≤3% | RC | PENDING | S4 |
| G6 | docs truthful; OWNER_SETUP single page | RC | PENDING | S6 |

## Anti-cheat / hygiene (S2, VERIFIED pre-merge)

- Secrets scan on all 4 branch diffs: clean.
- Scope: presence/orb = macos+docs only; hands = skills+tests+harness+docs+3 marked shared blocks; brain-1 = agent/cognitive/gateway/fabric/gateway + tests + comment-only .env edit.
- No branch touches contract_v1.json, pyproject.toml, uv.lock, compose.yaml, Makefile.
- Prod code: no test-id/fixture behavior branching (2 docstring mentions + diag.selftest's legitimate offline self-tests; testing.py header "never imported by production" now stale — P3 doc nit, RC to fix with NOTICE).
- No local-model imports in new prod code; OCR via Apple Vision helper.
