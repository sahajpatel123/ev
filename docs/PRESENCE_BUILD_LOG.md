# PRESENCE BUILD LOG — Evie native macOS face (branch evie-presence)

Owner: presence session. CLI 1 owns `docs/EVIE_BUILD_LOG.md`; this file is
mine and I never edit theirs. One 5-line entry per deliverable (done /
failing / assumptions / next) + recon and review entries. No merge, no
deploy, no push.

## RECON (2026-10-26)

- done: worktree `../evie-presence` on `evie-presence` from main `bf0412c`
  (newest; evie-acceptance fully merged). Read CLAUDE/AGENTS, macos/ in full,
  EVClient transports, fabric/approvals/receipt backend sources (read-only),
  BRAIN_DOSSIER, EVIE_BUILD_LOG tail. Wrote `docs/PRESENCE_RECON.md`.
- failing: none.
- assumptions: main stays the merge target; CLI 1 implements the D1
  server side; Command Line Tools only (no Xcode) so tests use the
  executable-checks pattern, not XCTest.
- next: D1 `docs/contracts/PRESENCE_EVENTS.md` + commit.

## D1 PRESENCE EVENT CONTRACT (2026-10-26)

- done: `docs/contracts/PRESENCE_EVENTS.md` — `evie-presence/1`, SSE GET
  `/v1/fabric/presence/stream`, 16 event types with JSON schemas, ordering /
  replay / versioning rules, worked example, CLI 1 conformance checklist.
- failing: none (docs only; no code to test).
- assumptions: CLI 1 implements the server side; `rich_confirm`,
  `receipt_feed`, `poll_fallback` are optional capabilities until then.
- next: D2 gesture engine (tests first).

## D2 GESTURE ENGINE (2026-10-26)

- done: `PresenceGesture` lib (pure Swift engine: hold/double-tap/Esc,
  chord guard, latch 10m/warn 9m, resync, suspend, side scope, default
  off) + `OptionGestureTap` listen-only CG adapter + minimal AppModel
  wiring (log-only). `swift run PresenceChecks`: 80/80 incl. 12,005-event
  typing replay with ZERO activations (+10k seed-2 replay, gesture control
  8/8). Full `swift build` green.
- failing: none. (`swift test`/XCTest does not exist on this toolchain —
  verified; the executable-checks pattern is the gate, per repo precedent.)
- assumptions: keycodes 58/61/53; tap never installed (P3) until the owner
  enables it after D7.
- next: D3 HUD pill (tests first).

## COORDINATION — CLI 3 orb/island (2026-10-26)

- done: received CLI 3's visual-identity note (`evie-orb` branch has no
  commits yet — read-only check). Pill refactored: no orb, no level meter,
  no pulse animation; plain hostable content views + `OrbSlotView` (28pt
  placeholder). Added `PresenceLevels` (local-only input/output levels for
  the orb) and the "Audio levels" note to `PRESENCE_EVENTS.md` (levels
  never cross the stream; `turn.listening.level` stays a coarse hint).
- failing: none.
- assumptions: CLI 3's `IslandHost.present(content:)` will host these
  content views unchanged; integration step recorded for merge readiness.
- next: finish D3 (AppModel wiring, snapshots, commit).

## D3 HUD PILL (2026-10-26)

- done: `PresenceCore` lib (16 wire event types, transport protocol, pure
  snapshot reducer, store, static pill views + orb slot, non-activating
  click-through panel controller, ambient monitor, local levels) + minimal
  AppModel wiring (pill hidden/idle until D6 transport). Checks 166/166:
  decode/reducer/wiring + 23 offscreen PNGs (all 8 states x light/dark,
  expanded, panic, reconnect, contrast) reviewed in this session.
- failing: none. Full `swift build` green (only pre-existing warnings).
- assumptions: CLI 3 hosts these content views in IslandHost; Review hook
  logs until D4 wires the card.
- next: D4 confirm card (tests first).

## D4 CONFIRM CARD (2026-10-26)

- done: policy (spoken-never matrix, rich gate, expiry), decision client
  (existing fabric routes + reverify retry, pure builder/parser), card
  model (physical vs spoken paths, single-decision), content view
  (recipient/text/diff/undo/risk/expiry), key-capable panel controller
  (Return/Esc, activation hooks), AppModel Review wiring. Checks 233/233
  incl. 18 card PNGs (send/first-contact/diff/sql/minimal/expired +
  approved/denied/failed, light/dark), reviewed.
- failing: none. Full `swift build` green (only pre-existing warnings).
- assumptions: ticket paths used verbatim; fabric-route fallback for
  minimal tickets; reverify shape `{token}` verified in backend source.
- next: D5 rewind timeline (tests first).

## COORDINATION — CLI 4 bridge/helpers (2026-10-26)

- done: received CLI 4's note (`evie-hands` owns bridge Accessibility/OCR
  ops). Already compliant: no presence edit touches `BridgeSocketServer`,
  `helpers/`, or `Sources/EVLifeHelper`; macos/ writes stay Presence files
  (`PresenceGesture`, `PresenceCore`, `PresenceChecks`, `OptionGestureTap`)
  + minimal wiring (`AppModel`, `MenuBarView`, `Package.swift`).
- failing: none.
- assumptions: none.
- next: finish D5 (menu wiring was just added; build+checks running).

## D5 REWIND TIMELINE (2026-10-26)

- done: timeline client on existing `runtime/actions` + fabric undo/
  restore routes (pure parsers), row model + pure filter (today/search/
  status/verified), model (refresh/restore with honest per-row results),
  content view in the menu panel next to Permissions (Rewind button).
  Checks 272/272 incl. 8 timeline PNGs (today/restored/empty/error,
  light/dark), reviewed. List→ScrollView: NSTableView can't render
  offscreen, so the shipped tree uses the snapshot-safe container.
- failing: none. Full `swift build` green (only pre-existing warnings).
- assumptions: unified `receipt_feed` stays CLI 1's (contract §4); action
  rows are read-only (rollback needs reverify — noted as a gap, not built).
- next: D6 mock + snapshots (tests first).

## D6 MOCK + SNAPSHOTS (2026-10-26)

- done: `FixtureTransport` + 5 scripts (6-step, confirm, failure/
  recovery, background, panic), real `SSEPresenceTransport` (resume,
  backoff, persisted cursor; socket never opened in checks), pure SSE
  parser. Checks 300/300: replay end-states, determinism, parser edge
  cases + 8 replay PNGs (pipeline-proven states incl. card from a
  replayed ticket), reviewed.
- failing: none. Full `swift build` green (only pre-existing warnings).
- assumptions: scripts use stable ids; live attach is a merge-step (2
  lines in AppModel, documented for readiness).
- next: D7 guided owner checks (tests first).

## D7 GUIDED OWNER CHECKS (2026-10-26)

- done: `presence-check` tool (calibrate/permissions/hud) + shared
  `GestureTapSource` (app adapter refactored onto it) + pure recorder /
  analyzer / permission report + `docs/PRESENCE_OWNER_CHECKS.md`. Checks
  319/319 (recorder, verdicts, report matrix, Codable round-trip). Tool
  usage path verified; flows are owner-run only, never executed here.
- failing: none. Full `swift build` green (only pre-existing warnings).
- assumptions: owner runs permissions→calibrate→hud after merge; the
  tool never auto-tunes (prints the enable command on PASS).
- next: reviewer subagent + merge readiness + checkpoint.

## REVIEW + READINESS (2026-10-26)

- done: fresh reviewer subagent verified scope PASS (114 files, zero
  outside `macos/`+`docs/PRESENCE_*`+contract+snapshots), `swift build`
  green, 319/0 checks with deterministic snapshots, 14 PNGs reviewed,
  P1–P7 PASS with citations, contract PASS. Fixed all four findings:
  (1) committing the readiness doc now, (2) event count 15→16 wire
  types, (3) timeline snapshot framing (live tree never clipped),
  (4) full looking-at line when expanded. Clean-build audit: zero
  warnings in presence files (fixed 2), pre-existing warnings untouched.
- failing: none.
- assumptions: none remain; merge command in `PRESENCE_MERGE_READINESS.md`.
- next: none — checkpoint and stop. Do not merge/install/deploy.
