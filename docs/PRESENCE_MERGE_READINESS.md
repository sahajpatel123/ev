# PRESENCE MERGE READINESS — evie-presence → main

Branch `evie-presence` (7 commits on main `bf0412c`), worktree
`../evie-presence`. All of D1–D7 is built, tested headless, and committed.
No merge, install, or deploy was performed — CLI 1 or the owner merges with
the command at the bottom.

## 1. What was built

- **D1 contract** `docs/contracts/PRESENCE_EVENTS.md` (`evie-presence/1`):
  SSE `GET /v1/fabric/presence/stream`, 16 event types with JSON schemas,
  ordering/replay/versioning, worked example, CLI 1 conformance checklist,
  audio-level note (orb levels are local-only).
- **D2 gesture engine**: pure-Swift `GestureEngine` (hold/double-tap/Esc,
  chord guard, latch 10 min/warn 9 min, resync, suspend, side scope,
  default off) + shared listen-only `GestureTapSource` + thin `OptionGestureTap`
  adapter with minimal AppModel wiring (log-only).
- **D3 HUD pill**: `PresenceCore` (16 wire event types, `PresenceTransport`
  protocol, pure snapshot reducer, store) + static pill views with the CLI 3
  orb slot + non-activating click-through panel controller + ambient
  `looking_at` monitor + local `PresenceLevels`.
- **D4 confirm card**: spoken-never policy, decision client on the existing
  fabric approve/deny routes (reverify retry, 404/409 mapped), card model
  with separate physical/voice paths, exact-render content view, key-capable
  panel (Return/Esc), Review wiring from the pill.
- **D5 rewind timeline**: existing `runtime/actions` + fabric undo/restore
  routes behind pure parsers, filterable model (today/search/status/
  verified), content view in the menu panel next to Permissions.
- **D6 mocks**: `FixtureTransport` + 5 scripts (6-step, confirm, failure/
  recovery, background, panic), real `SSEPresenceTransport` (resume,
  backoff, persisted cursor; socket never opened in checks), pure SSE parser.
- **D7 owner checks**: `presence-check` tool (calibrate/permissions/hud,
  owner-run only) + pure calibration analyzer/report + `docs/PRESENCE_OWNER_CHECKS.md`.

Supporting docs: `PRESENCE_RECON.md`, `PRESENCE_BUILD_LOG.md` (per-deliverable
entries + CLI 3/CLI 4 coordination notes), `PRESENCE_OWNER_CHECKS.md`, this file.

## 2. Evidence

- `swift build` (macos/): **Build complete**, only pre-existing warnings in
  untouched files (`EvieRelayForwarder`, `LiveConversation`, `ExplicitInterruptMonitor`).
- `swift run PresenceChecks`: **319 passed / 0 failed**, covering the gesture
  state machine, both typing replays, all 16 event-type decodes, the store reducer,
  policy/client/parser/filter pure logic, model flows against fakes, SSE parsing,
  replay end-states + determinism, calibration/report logic, source wiring, and
  every snapshot's file assertions. (`swift test`/XCTest does not exist on the
  Command Line Tools toolchain — verified 2026-10-26 — so the repo's own
  executable-checks pattern is the gate, per precedent.)
- **57 PNGs** under `docs/presence-snapshots/`, all reviewed in-session:
  8 pill states × light/dark + expanded/panic/reconnect/contrast (23),
  9 card states × light/dark (18), 4 timeline states × light/dark (8),
  4 replay-provenance states × light/dark (8).
- Zero-false-activation: **12,005-event** seeded typing replay with heavy
  Option use (1,334 chord blocks, 223 taps) → **0 activations, 0 outputs**;
  second 10,000-event seed also zero; gesture-positive control fires 8/8
  with exact reasons, proving the machine is not dead.
- Scope: `git diff --stat main...evie-presence` = 114 files, +9338/−1, and
  **zero files outside** `macos/`, `docs/PRESENCE_*.md`,
  `docs/contracts/PRESENCE_EVENTS.md`, `docs/presence-snapshots/`.
- Live effects: none. No install, signing, launchd, :8000, GUI, tap, mic,
  model, push, or merge. Owner-run flows were compiled + usage-verified only.

## 3. Contract version

`evie-presence/1`, additive-only. CLI 3 coordination is folded in (audio
levels local-only; content views host unchanged in `IslandHost` or the
floating panel; 28pt orb slot reserved).

## 4. What CLI 1 must provide (server side)

1. `GET /v1/fabric/presence/stream` (SSE + `Last-Event-ID` replay from a
   ≥1000-frame/30-min ring) + `stream.hello` first + `:heartbeat` ≤15 s.
2. All 15 event payloads per §3 (causal per-turn order; terminal frames
   never coalesced; stable ids; at-least-once).
3. `rich_confirm`: full `recipient`/`text`/`diff` on `turn.needs_confirm`
   (until then the card renders label-only with approve disabled — by design).
4. `receipt_feed`: `GET /v1/fabric/turns` (timeline shows actions + file
   restores until then).
5. `poll_fallback`: `GET /v1/fabric/presence/since` (optional, degraded mode).
6. Server-side R7: reject voice-channel approves of HIGH_RISK tickets even
   if a client bug sends one (contract §6 checklist).

## 5. Integration steps (post-merge, owner/CLI 1)

1. **Wire the live stream** (2 lines in `AppModel.start()`, after the D3 block):
   `presenceStore.attach(SSEPresenceTransport(baseURL: config.baseURL, token: config.apiKey, deviceId: <registry uuid>))`.
   Device id: the existing device-registry uuid the app already holds.
2. **Gesture setting**: owner runs `presence-check permissions` →
   `calibrate` → on PASS runs the printed
   `defaults write com.ev.suit EV_GESTURE_ENABLED -bool true`, restarts the
   app, then runs `presence-check hud`. Stock thresholds fit unless the
   report says otherwise.
3. **Install**: build + install with the EXISTING identity (`macos/scripts/
   package.sh`, "EV Code Signing" via `signing.sh`) so TCC grants survive;
   never change the signing identity. Do not load/unload launchd jobs
   outside the normal deploy wrapper.
4. **CLI 3 handoff**: replace `OrbSlotView` with `EvieOrbView` (or host pill
   content in `IslandHost.present(content:)`); feed the orb from
   `PresenceLevels` (wire the mic tap → `updateInput`, TTS meter →
   `updateOutput`).
5. **CLI 4 note**: bridge/helpers untouched — no conflicts expected there.

## 6. Known gaps (honest, not hidden)

- Action rollback is not offered on timeline rows (needs reverify + is
  destructive); file restores work, everything else is read-only.
- Until `rich_confirm` ships, the card cannot approve (label-only by design).
- Until `receipt_feed` ships, the timeline shows actions + file restores only.
- Gesture turn-driving is log-only: `handleGestureOutputs` needs the brain-loop
  entry (CLI 1's turn API) to start turns from hold/latch.
- `presence-check hud` needs a display session; `calibrate` needs the owner.
- XCTest gate is impossible on this toolchain; the executable-checks gate
  substitutes (repo precedent, documented in D2/D3 log entries).

## 7. Merge command (owner/CLI 1 runs this, not presence)

```sh
cd /Users/sahajpatel/Code/ev
git checkout main
git pull --ff-only
git merge --no-ff evie-presence -m "Merge branch 'evie-presence': native macOS presence face (D1-D7)"
make baseline-write   # presence adds Swift targets + docs; re-measure §5
swift run --package-path macos PresenceChecks   # expect 319/0
```

After merge: run the D7 flows in `docs/PRESENCE_OWNER_CHECKS.md`, then delete
the worktree (`git worktree remove ../evie-presence`) and the branch.
