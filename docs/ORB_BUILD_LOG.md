# ORB BUILD LOG (CLI 3 only)

## 2026-10-26 — Recon + worktree
- Done: worktree `../evie-orb` on branch `evie-orb` from `main` (`bf0412c`,
  newer than `evie-acceptance`); read CLAUDE.md/AGENTS.md, macOS build, audio
  taps, `evie-presence/1` contract; wrote `docs/ORB_RECON.md`.
- Failing: nothing yet (no code written).
- Assumptions: `main` is the correct base (newest); CLI 2 stays within its
  listed files; `offline` has no presence event (derived locally).
- Next: package skeleton + R2/R3 (state machine, envelope, level source).

## 2026-10-26 — R2 state machine + R3 level source
- Done: `macos/Packages/EvieOrb` (own Package.swift, macOS 14): OrbState,
  OrbParameters (spring targets), OrbStateMachine (critically damped,
  momentary success/error), EnvelopeFollower (30/250 ms, gate, log),
  OrbLevelSource (vDSP RMS, atomic meter, synthetic), PresenceMapping table.
- Failing: nothing. `swift run EvieOrbCheck`: 151 passes, 0 failures.
- Assumptions: checks-as-executable (repo idiom) — this Mac has CLT only, no
  XCTest and unresolvable swift-testing macros; `swift test` cannot run here.
- Next: R1 shader + headless OrbRenderer.

## 2026-10-26 — R1 procedural renderer
- Done: original `orb.metal` (polar domain-warped fbm ring, tangential flow,
  dark core, premult alpha, light-mode groove+halo) + headless `OrbRenderer`
  (offscreen BGRA, CPU/GPU ms) + preview tool still/states/sequence modes.
- Failing: nothing. Checks 164/164; visually verified all 9 states dark+light;
  tamed listening energy gain, added dark groove for light legibility.
- Assumptions: BGRA row order renders top-first consistently to PNG.
- Next: R4 frame pacing + R5 benchmark harness + ORB_PERFORMANCE.md.
