# Release captain log (rocky-sculptor, CLI 5)

## Messaging mechanism (S0, verified 2026-10-27)

Direct session messaging works via `send_session_message` by session name
(target = `blooming-mensa` etc.). PING to blooming-mensa returned
`accepted`/`sent` with `delivery_unconfirmed`; the peer's ACK arrived as a
runtime-context session message within ~2 minutes. Receipts stay
`delivery_unconfirmed` even on success — treat a peer reply as the real ACK.
Fallback mailbox created at `~/.ev/mailbox/<session>/{inbox}` (atomic
write-temp-then-rename); peers told to check it at step boundaries.

## S0 ORIENT (2026-10-27 ~02:50 IST)

Inventory (measured, this session):
- main @ `d6cba41` (docs-only head; tree has 2 untracked scratch items not
  mine: `backend/eval/mimo-smoke.json`, `tmp/re-grokbot-openbot/`).
- Peer branches vs main: `evie-brain-program` 0 ahead (fully merged);
  `evie-brain-program-1` 4 ahead / 11 behind (19 files, +784/-70);
  `evie-presence` 8 ahead (115 files, +9497); `evie-orb` 8 ahead (78 files,
  +4901); `evie-hands` 27 ahead (155 files, +24612).
- Peer worktrees: /tmp/ev-brain-1 (CLI1 fix branch), /Users/sahajpatel/Code/evie-{presence,orb,hands}.
- Live services: ev.api :8000 (pid 1611) + ev.talk :18000 (pid 1615), both
  report git sha `d6cba41`, started 2026-10-26T20:34Z (AFTER the 20:09 IST
  skew incident — restart already happened; skew hypothesis needs re-test
  with a live turn, not assumed fixed). Brain id in /v1/health:
  `meta/muse-spark-1.3-contributor` (turn_control + manager).
- GUI lock: free (`~/.ev/locks/gui` absent; log exists).
- Integration worktree: `../evie-rc1` branch `evie-rc1` @ `d6cba41`.
- Key credit: CLI1 reports $0.51 of $5; CLI4 reports $0.21 of $5 at a later
  time. Credit is nearly exhausted — real-brain stages will need sampling
  and an OWNER-ACTION to raise the key limit. VERIFY_BUDGET_USD=15 set,
  but key credit is the binding constraint.

Peer status (FREEZE sent to all 4 via direct message):
- blooming-mensa (CLI1): IDLE, FREEZE acked. main @ d6cba41 + fix branch
  evie-brain-program-1 @ 71e2e0f. Known failures: (1) live skew incident
  (see above), (2) fabric-gate 2 pre-existing fails (HEAD-identical),
  (3) full suite 11 pre-existing fails (HEAD-identical, 0 regressions).
- light-setebos (CLI4): IDLE. evie-hands @ 50699fc. Claims CUG 255/255,
  234 offline tests + ruff + mypy clean. Gaps: live-Chrome re-run blocked
  by other headless Chromes; real-brain CUG not run (key credit);
  untrusted-click latency 1.6-1.8s misses 1.5s target.
- coastal-thalassa (CLI3): IDLE. evie-orb @ da3dfc8. Claims 263/263
  checks green, reviewer READY TO MERGE. Gaps: app built never launched,
  integration left to others by design.
- terra-orbit (CLI2): NO REPLY YET (as of this entry). Treated as idle
  after 30 min per brief §1.5.

## S3 INTEGRATE (2026-10-27; rc1 @ 45a6a62)

- Merges (no-ff, gates re-run after each): brain-1 47/47, presence 319/0,
  orb 263/0, hands 234/234 + ruff + mypy (1 env-only mypy error fixed on
  rc1: imaging.py Image annotation; NOTICE sent, CLI4 concurred).
- Conflicts: .env.example + ENVIRONMENT.md only. Kept main's purge removal
  of retired-var docs + hands' HANDS blocks (by intent; NOTICE to CLI4).
- Seams built + tested: presence_bus (evie-presence/1 server: stream/since
  endpoints, kernel/agent/park/panic/background hooks; 12 tests),
  skills.act + world.query tools (dispatch + specs + policy gate; 7 tests),
  GUI lock on live-proof + mac-control-live, SSE attached in app, island
  hosts live plan/caption, orb dual-ingests real PCM taps, levels bridged.
- Swift: EV target builds (warnings pre-existing only), 319/0 + 263/0 kept.
- EV.app built once with "EV Code Signing" (EVGitSHA=45a6a62), not installed.
- Note: BuildInfo.swift is rewritten by the build script (generated churn,
  left uncommitted). Live tree backend/ untouched (frozen per incident rule).

### S3 status

- done: 4 merges + all 5 seams + app build, all gates green at 45a6a62.
- failing: none on rc1.
- assumptions: QueueGate (in-process) approvals stay off the HUD card (card
  drives REST tickets only); agent-run approvals surface via API/phone.
  Confirm-card auto-show NOT added (peer's pill->Review design kept).
- next: S4 dynamic verification from frozen worktree + isolated :18005.

## S1+S2 (2026-10-27 ~04:00 IST)

- terra-orbit also idle-confirmed (evie-presence @ 01e103a, 319/0). All 4 peers frozen+idle.
- Claims extracted by 3 subagents (main/hands/presence+orb reports) into docs/CLAIMS_LEDGER.md.
- Cheap evidence re-run by me: CUG JSONs 146/146+34/34+63/63+12/12 (fs=sv=0), corpus 0.52%/118 panes, PNGs 57/23/18, skills 30 files, tags, ancestors — all match prose (ARTIFACT-MATCH).
- Branch gates re-run by me on exact commits: brain-1 47/47 + ruff + mypy; hands 234/234 + ruff + mypy; orb 263/263; presence build green + 319/0, warnings pre-existing only. ALL VERIFIED.
- S2 static: secrets clean; scope clean; no contract/dep touches; no R8 violation (one P3 doc nit: testing.py header stale after legit diag.selftest import — fix on rc1 post-merge with NOTICE); no local-model imports; OCR=Apple Vision helper.
- Gaps confirmed already: T2 n=15 latency dataset not committed (UNVERIFIABLE, must re-run); AX-fallback 1.6-1.8s misses 1.5s (admitted); real-brain CUG not run ($0.21 left); LP6 panes conflict (honest gap); GAUNTLET_REAL_BRAIN truncated + harness-only (admitted in HANDOFF).
- /tmp/ev-brain-1 worktree dir is gone (branch intact); verified via /tmp/rc-brain1 instead.

### S0 status

- done: rules read; messaging verified two-way; FREEZE to all; 3/4 peers
  idle-confirmed; inventory recorded; evie-rc1 worktree created.
- failing: none yet (verification not started).
- assumptions: peer HEADs as reported match the branches (to verify via
  git); live skew incident possibly resolved by the 20:34Z restart
  (to verify with a live turn).
- next: S1 claims audit (spawn extraction agents); S2 static verification
  per branch; chase terra-orbit reply.
