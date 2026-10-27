# Release report (release captain, rocky-sculptor)

Branch: evie-rc1. Frozen verification HEAD: 21df36c (journeys/soak/live lanes).
Docs: TRACEABILITY.md (verdicts), GOLDEN_JOURNEYS.md (J1–J16), CLAIMS_LEDGER.md,
RELEASE_STATE.md, DEPLOY_RECORD.md (S6), OWNER_ACCEPTANCE.md (via `ev accept`).

## Verdict: CONDITIONAL GO (pending full-suite re-run + S6 install)

P0 = 0. P1 = 0 open (3 found, 3 fixed with regression tests). The release
candidate is honest and safe: every failure found fails closed, every receipt
verified is truthful, and every gap is named below — nothing is disguised.
Remaining work is install/owner-side verification (S6+S7), budget-blocked
probes (scorecard/FRESH/latencies), and queued P2s.

## Per-area counts (VERIFIED / PARTIAL / OWNER-ACTION / NOT-BUILT-in-partial)

- A foundation/safety: 4 VERIFIED (A1 A3 A4 A7), 6 PARTIAL (A2 A5 A6 A8 A9 A10)
- B brain: 1 VERIFIED (B6), 7 PARTIAL (B1 B2 B3 B4 B5 B8 B9), 1 OWNER-ACTION (B7)
- C voice: 1 PARTIAL (C2), 2 OWNER-ACTION (C1 C3)
- D presence: 2 VERIFIED (D1 D5), 3 PARTIAL (D2 D3 D4 — need installed app)
- E orb: 1 VERIFIED (E2), 2 PARTIAL (E1 E3 — need installed app)
- F hands: 2 VERIFIED (F4 F5), 4 PARTIAL (F1 F2 F3 F6)
- G system/ops: 0 VERIFIED, 5 PARTIAL (G1 G2 G4 G5 G6), 1 OWNER-ACTION (G3)

NOT-BUILT items inside partials: voice claim monitor; `ev notebook`;
`ev brain score`; read_result; hard delete (R14, pre-existing);
turn.listening/transcript backend publishers.

## Refuted claims / fixed in S4+S5 (all with regression tests + peer NOTICE)

1. live.t2.untrusted_click_a (T2 AX-fallback) failed 3/3: AX tree lags DOM
   ~2.5s on fresh tabs; single-shot find missed and the receipt falsely
   claimed "I pressed skip" with rung=pointer. Fix: poll + honest rung=dom.
2. Documented `ev hands check ...` / `scripts/ev-hands check ...` rejected
   by argparse. Fix: strip leading `check`.
3. S21 contract oracle 578 stale by the 2 recorded presence endpoints.
   Additive-only re-proven (550 intact, 0 removed/changed); literal →580.
4. evvision OCR helper unbuilt on rc1 → T1 grounding blind; corpus tests
   inject ocr_lines so the gap hid. Built; S6 must build it (release step).

## Headline numbers (my runs only)

- Suite: 5899 passed + triage (9 fixed, 2 pre-existing: oauth flaky, PWA
  churn); FULL re-run after S5 pending. Eval gates 25/25, 145/145 checks.
- Gauntlet real-brain DEV: 535/535, 87.66% pass, 0 unauth, 0 false-success,
  53% hard; spent $0.09. Mutants 25/25 killed.
- CUG: 255/255 (146 sim + 34 corpus + 63 live + 12 live_term), 0/0.
- T2: 17/17 live; DOM click p95 57ms post-control (n=10); AX path slow.
- T1: corpus 34/34; live build audit err 0.0007; executable-resume S7.
- Gesture: 12005 events, 0 activations. PresenceChecks 319/0. Orb 263/263.
- Orb 120pt: static 0f/0.004%; animated ≤1.29% CPU; GPU max 0.94ms, 0 spikes.
- Watcher polling: 0.10% of one core / 40s. Panic engage 26ms.
- Live B1: kind=brain, meta/muse-spark-1.3-contributor, $0.000353/turn-scale.
- Soak (15-min bounded, :18005): 144 reqs, 0 errors; turn p50 ~13ms;
  kill→recovered 2.9s (≤10s), data survived; tmux kill-server no orphans;
  RSS 80.7→87.2MB bounded, fds 97→96, CPU 0.0-0.3%. 2hr NOT run.

## Remaining P2 (queued into `ev improve`)

AX-fallback latency; B4 surface 46-vs-14-18 + read_result; notebook;
claim monitor; hard delete; transcript presence frames; corpus-OCR bypass
hole; egress-100% + cost-±5% proofs; barge-in timings; idle CPU app-side;
EV_FABRIC=0 legacy path; shell-session live drill.

## Rollback and kill switches

- S6 backups (state, /Applications/Evie.app, DB dump, launchd plists) before
  any install; merge no-ff; tag evie-v1.0; deploy script; post-deploy subset;
  automatic rollback on failure (see DEPLOY_RECORD.md).
- `ev panic` (26ms, verified live) + EV_FABRIC=0 legacy path.

## Owner steps

1. Raise the OpenRouter key limit (OWNER-ACTION; ~$0.02 left blocks
   scorecard/FRESH/latency probes).
2. Approve S6 install, or run it: merge → tag → install → deploy → verify.
3. Run `ev accept` and work the checklist (gesture, voice, orb, T1, T2,
   iPhone, live send, background report).
