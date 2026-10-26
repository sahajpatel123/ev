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

### S0 status

- done: rules read; messaging verified two-way; FREEZE to all; 3/4 peers
  idle-confirmed; inventory recorded; evie-rc1 worktree created.
- failing: none yet (verification not started).
- assumptions: peer HEADs as reported match the branches (to verify via
  git); live skew incident possibly resolved by the 20:34Z restart
  (to verify with a live turn).
- next: S1 claims audit (spawn extraction agents); S2 static verification
  per branch; chase terra-orbit reply.
