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
  `/v1/fabric/presence/stream`, 15 events with JSON schemas, ordering /
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
