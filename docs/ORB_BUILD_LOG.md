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

## 2026-10-26 — R4 frame pacing (+ R5 harness, bench running)
- Done: `FramePacingPolicy` (60/30/0 fps table, 15 in Low Power, static link
  stop, visibility gating) + `FramePacer` (CVDisplayLink, fps thinning);
  renderer split into texture/readback paths; `bench` mode (process CPU %,
  per-frame CPU, GPU timestamps); `macos/scripts/orb-benchmark`.
- Failing: nothing. Checks 201/201 incl. headless display-link ticks.
  Smoke: idle 0 frames/0.08% CPU; listening 60fps@120pt 1.41%/0.16ms GPU.
  Full 60s matrix running in background (~36 min).
- Assumptions: Low Power = 15 fps cap (not full pause — keeps voice feedback).
- Next: R6 island host.

## 2026-10-26 — R6 notch island host
- Done: pure `IslandLayout` (notch-beside compact, drop-down island, 3
  placements, fullscreen hide unless needs_confirm, clamps) + `IslandHost`
  NSPanel (non-activating, status-bar level, all Spaces, click-through
  compact) + `EvieOrbView` (CAMetalLayer live path, no-Metal fallback ring)
  + `IslandContent` protocol + SwiftUI `OrbView` + placeholders.
- Failing: nothing. Checks 260/260. Notch rule grounded on this 16" M2's
  live aux-area values (208 pt gap, safeArea top 37.5).
- Assumptions: notch-beside = right side; identical-size twin screens both
  hide on fullscreen (documented edge).
- Next: R7 previews + R8 owner app.

## 2026-10-26 — R7 previews + R8 owner check
- Done: tool `tour`/`sidebyside`/`island` modes (real pixels+layout+views);
  `orb-render-previews` renders 23 MP4s + 18 stills to docs/orb-previews/;
  `OrbPreviewApp` (state picker, I/O sliders, gated mic, island + presence
  buttons, live CPU/fps/GPU) + `orb-preview` script + ORB_OWNER_CHECK.md.
- Failing: nothing. Verified clip durations, bloom→settle + breathing
  strips; app BUILT only, never launched (O4).
- Assumptions: island wallpaper/menu-bar/notch are drawn mocks (stated).
- Next: R5 numbers → ORB_PERFORMANCE.md, then review + merge-readiness.

## 2026-10-26 — R5 bench v1 (miss) → texture-reuse fix
- Done: full 60s matrix v1 (per-frame texture alloc): static 0 frames ✓,
  GPU means ≤0.5ms ✓, but 60fps CPU 2.1–2.3% MISSED ≤2% on 4 runs.
  Fix: `renderTexture(reusing:)` recycles one texture per run (live path
  recycles drawables — the alloc was harness-only cost); + GPU warmup split
  (raw max vs steady max) + link-stopped deployed-idle runs. Re-run live.
- Failing: nothing in code; v1 numbers kept at /tmp/bench-v1-*, never hidden.
- Assumptions: none — methodology change documented in ORB_PERFORMANCE.md.
- Next: v2 numbers → ORB_PERFORMANCE.md, reviewer, merge-readiness.
