# Traceability (release captain, rocky-sculptor)

Status per §2 master requirement on evie-rc1 (HEAD 21df36c unless noted).
VERIFIED only from my own re-runs. Owners: B=blooming-mensa, P=terra-orbit,
O=coastal-thalassa, H=light-setebos, RC=me.

## A. Foundation and safety

| ID | Requirement | Owner | Status | Evidence |
| --- | --- | --- | --- | --- |
| A1 | Policy single authority; audit hash chain verified | B | VERIFIED | test_pol_policy 56/56; R8 live (email→CONFIRM); audit.verify ok, 4678 entries; skills_dispatch 7/7 |
| A2 | Receipts on every effect; claims only from verified receipts | B/H | PARTIAL | receipts+undo live (files/SQL/T2/comms); T2 false "I pressed skip" receipt FIXED (rung=dom honesty); voice claim monitor NOT-BUILT |
| A3 | Untrusted never instructs; injection blocked | B | VERIFIED | S28 13/13; filter/comms 39/39; R8 live; mutants 25/25; DEV 0 unauth; comms keyword catcher shallow (5 patterns, innermost tripwire only) |
| A4 | Files: explicit roots, snapshots/undo, Trash, CONFIRM, DENY | B | VERIFIED | live: create→trash→undo-restore; outside-root→CONFIRM; secret path DENY |
| A5 | Shell sessions; SQL lane; comms draft→confirm→send→verify; grants off | B | PARTIAL | SQL live (read AUTO, preview rollback, confirm, prod DENY); comms park/approve 4/4; shell-session live drill missing; grants in policy tests |
| A6 | `ev panic` ≤2s; EV_FABRIC=0 legacy works | B | PARTIAL | panic live (engage 26ms, deny-while-panicked, clear); EV_FABRIC=0 path not tested |
| A7 | 550 paths intact; additions recorded; settings drift clean | RC | VERIFIED | 550/550 byte-identical, +30 recorded (0 removed/changed); drift+contract 6/6 (S21 count 578→580, G3-logged) |
| A8 | egress ledger 100%; capture exclusions; hard deletes; secrets clean | B/H | PARTIAL | secrets clean (S2); exclusions live+tests (banking/private/secure-field); egress brain-rows exist, 100% unverified; hard delete NOT-BUILT (R14 gap, pre-existing) |
| A9 | two-model only; no local weights; doctor; no double speaks; models verify | B | PARTIAL | no local imports (S2); echo-never-speaks live (J13); doctor honest-degraded (bridge down); models-verify + models-dir-empty not run |
| A10 | `ev keys`; cost ledger ±5%; caps + honest degradation | B | PARTIAL | keys/cost CLIs work (presence-only); ±5% vs provider + caps unverified; key credit ~$0.02 binding |

## B. Brain

| ID | Requirement | Owner | Status | Evidence |
| --- | --- | --- | --- | --- |
| B1 | rc1 runs with Muse brain; effort policy | B | PARTIAL | :18006 kind=brain, meta/muse-spark-1.3-contributor, trace (VERIFIED); effort policy (medium/high by behavior) not probed |
| B2 | one loop: no silent turns, fan-out cap, plan/verify/recovery/budgets/delegate/steer | B | PARTIAL | Phase-0 47/47; fanout 13/13; no-silent 16/16; _record_thread_completion wired; budgets/steering individual proofs thin |
| B3 | living thread; compaction; notebook; recall; caching | B | PARTIAL | recall+foundation 56/56; J6 live (write→recall→tombstone→excluded); notebook NOT-BUILT; 200-turn + caching unmeasured |
| B4 | tool surface wired to SKILLS_API; structured results + read_result | B/H | PARTIAL | 46 semantic tools (brief said 14-18: DEVIATION, surface grew); skills.act/world.query wired+tested; truncation partial; read_result NOT-BUILT |
| B5 | doctrine behaviors | B | PARTIAL | DEV 535 real-brain behavioral (87.66%, 0 safety fails); doctrine items not individually probed |
| B6 | turn traces + `ev trace`; brain_* telemetry | B | VERIFIED | presence started/thinking/progress/failed per turn live; emitter conformance 15/15; ev trace + rc_commands tests 5/5 |
| B7 | scorecard ≥120 probes; penalty; memory ≥90%; n + CI | B | OWNER-ACTION | budget $0.02 of $5; not run. Needs "raise key limit" |
| B8 | mutants ≥90%; ≥35% hard; DEV+FRESH; 0/0 | B | PARTIAL | mutants 25/25; DEV 535 (87.66%, 53% hard, 0 unauth, 0 false-success); FRESH not run (budget) |
| B9 | all channels reach One Mind + same thread | B | PARTIAL | CLI/chat/cognitive live; phone route 19/19; PWA/voice-WS/Shortcuts/routines not live-traced |

## C. Voice

| ID | Requirement | Owner | Status | Evidence |
| --- | --- | --- | --- | --- |
| C1 | Gemini Live path; non-blocking turns; reconnect; mic gating | B | OWNER-ACTION | no live provider verification (budget); owner call in S7 |
| C2 | barge-in ≤300ms/≤2s; claim monitor ≥95%; spoken yes never approves | B | PARTIAL | spoken-yes refusal tested (C2) + live policy; barge-in timings unmeasured (logic only); claim monitor NOT-BUILT |
| C3 | TTFA/first-token p50/p95; 1500ms budget truth | B | OWNER-ACTION | budget; prior honest over-budget numbers stand (3945/12358ms) |

## D. Presence

| ID | Requirement | Owner | Status | Evidence |
| --- | --- | --- | --- | --- |
| D1 | gesture + 0 activations/≥10k replay; secure-input; setting-gated | P | VERIFIED | 12005 events, 0 activations (+seed2); PresenceChecks 319/0 on rc1; owner calibration in S7 |
| D2 | HUD states/plan/looking-at; no focus steal | P | PARTIAL | snapshots in 319/0; live HUD needs installed app (S6) |
| D3 | confirm card; approvals API; double-approve/stale | P | PARTIAL | model+snapshot checks in 319/0; live card needs app |
| D4 | rewind timeline + undo | P | PARTIAL | checks in 319/0; live needs app |
| D5 | server-side presence stream per contract | P/B | VERIFIED | emitter conformance 15/15; version+types pinned; live frames per turn incl plan/failed/needs_confirm paths |

## E. Orb

| ID | Requirement | Owner | Status | Evidence |
| --- | --- | --- | --- | --- |
| E1 | shader/states/audio-reactive/a11y | O | PARTIAL | EvieOrbCheck 263/263 on rc1; level-seam wired (S3); live audio-reactive + ReduceMotion need app |
| E2 | 0 frames idle; ≤2% CPU; ≤1.5ms GPU @120pt | O | VERIFIED | rc1 headless: static 0f/0.004%; animated ≤1.29% CPU; GPU max 0.94ms, 0 spikes (full-app measure at S6) |
| E3 | island placement/expand/focus/fullscreen | O | PARTIAL | HostChecks in 263/263; live displays need app |

## F. HANDS

| ID | Requirement | Owner | Status | Evidence |
| --- | --- | --- | --- | --- |
| F1 | SKILLS_API wired to brain tools; world model queries | H/B | PARTIAL | dispatch 7/7; world 5/5; 44 skills registered; live "which tab" probe pending browsers |
| F2 | T1 ≥90% exact, ratios ±8%, ids exact-or-flagged, confirm, read-back | H | PARTIAL | corpus 34/34; live build audit err 0.0007; grounding honesty live; evvision built on rc1 (was missing); live executable-resume S7 (frozen-surface env block) |
| F3 | T2 ≤1.5s p95; honest edge cases; phone channel; watcher | H | PARTIAL | live 17/17; DOM click p95 57ms; AX-fallback slow-success (over budget, admitted); AX-race FIXED; watcher live; phone composition S7 |
| F4 | CUG ≥150: ≥90% fixtures, ≥85% live, 0/0 | H | VERIFIED | 255 tasks (146 sim + 34 corpus + 63 live + 12 term), 100%, 0 false-success, 0 scope violations |
| F5 | learned ≥80%, self-heal ≥70%, never from untrusted | H | VERIFIED | live.learn 12/12 (compile/replay/heal×4/never-learn×2/novel×5); n=12 noted |
| F6 | watchers idle ~0, ≤1% polling, low-risk only | H | PARTIAL | 0.10% of one core over 40s polling; idle-unpolled + low-risk policy unverified |

## G. System/ops

| ID | Requirement | Owner | Status | Evidence |
| --- | --- | --- | --- | --- |
| G1 | Evie.app builds once, signs, TCC, login item, bridge | RC | PARTIAL | built once (EV Code Signing, EVGitSHA), not installed; TCC/login/bridge at S6 |
| G2 | all `ev` subcommands behave | B/H/RC | PARTIAL | setup/doctor/permissions/keys/cost/trace/privacy/gauntlet/hands-check VERIFIED live; hands-check spelling FIXED; models-verify unrun; notebook + brain-score NOT-BUILT; improve unverified |
| G3 | iPhone PWA/channels; Tailscale Serve on/Funnel off | B | OWNER-ACTION | needs real phone + network; cellular explicitly owner |
| G4 | suite: no new failures; eval green except listed skips | RC | PARTIAL | baseline triaged (9 fixed, 2 pre-existing); eval 25/25; FULL re-run after S5 fixes pending |
| G5 | soak + chaos; idle CPU app ≤1% backend ≤3% | RC | PARTIAL | 15-min bounded soak + kill/tmux chaos running (2hr not run); idle numbers pending |
| G6 | docs truthful; OWNER_SETUP single page | RC | PENDING | this file + report in progress |

## Anti-cheat / hygiene (S2, VERIFIED pre-merge)

- Secrets scan on all 4 branch diffs: clean.
- Scope: presence/orb = macos+docs only; hands = skills+tests+harness+docs+3 marked shared blocks; brain-1 = agent/cognitive/gateway/fabric/gateway + tests + comment-only .env edit.
- No branch touches contract_v1.json, pyproject.toml, uv.lock, compose.yaml, Makefile.
- Prod code: no test-id/fixture behavior branching (diag.selftest's offline self-tests legitimate).
- No local-model imports in new prod code; OCR via Apple Vision helper (built on rc1 in S4).

## S4/S5 fixes by RC (all with regression tests + NOTICE to owner)

1. T2 AX-fallback race: single-shot ax.find vs ~2.5s AX lag + false "I pressed skip" receipt → poll 5×0.5s + honest rung=dom (H, NOTICE sent).
2. `ev hands check ...` documented spelling rejected → strip leading `check` (H, NOTICE sent).
3. S21 contract count 578→580 for recorded presence endpoints (G3-logged oracle maintenance).
4. evvision built on rc1 (env fix; S6 release step).
