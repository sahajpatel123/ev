# ORB MERGE READINESS (CLI 3 → main)

Branch `evie-orb` (worktree `../evie-orb`, based on `main` `bf0412c`) is
ready to merge. No merge/push/deploy performed by CLI 3 per O5.

## 1. What was built

A self-contained Swift package `macos/Packages/EvieOrb` (own `Package.swift`,
macOS 14, zero dependencies) — Evie's procedural visual identity:

- **R1 renderer:** original clean-room `orb.metal` (polar domain-warped fbm
  ring, tangential wisps, dark core, premultiplied alpha, light-mode groove
  halo) + headless `OrbRenderer` (offscreen BGRA, CPU/GPU ms per frame).
  All spec uniforms: time, energy, radius, thickness, turbulence, flow
  speed, rotation, glow, tint, state blend (+ halo/opacity/orbit/bloom/
  coolDim/confirmPulse/octaves/reduceMotion).
- **R2 motion:** 9-state `OrbStateMachine` with critically damped springs
  (no snapping), 30/250 ms `EnvelopeFollower` (noise gate, log scaling),
  momentary success/error with auto-decay, sticky needs_confirm.
- **R3 audio:** `OrbLevelSource` protocol; `AtomicOrbLevelMeter` (vDSP RMS,
  one atomic Float, fed by the app's existing taps); `SyntheticLevelSource`
  and `ManualLevelSource` for tests/previews/UI.
- **R4 pacing:** `FramePacingPolicy` (60 fps listening/speaking, 30
  thinking/acting/confirm/success/error, 0 static, 15 in Low Power Mode) +
  `FramePacer` (CVDisplayLink, fps thinning, visibility gating). Static
  settled orbs STOP the link: zero frames, ~zero CPU. Reduce Motion freezes
  flow/rotation (brightness still follows level); Reduce Transparency
  flattens the ring.
- **R5 proof:** headless `bench` harness (process CPU %, per-frame CPU,
  GPU timestamps) + `macos/scripts/orb-benchmark`. See §2 and
  `docs/ORB_PERFORMANCE.md`.
- **R6 host:** pure `IslandLayout` (notch-beside compact orb, drop-down
  island, 3 placements, fullscreen hide unless needs_confirm, clamps) +
  `IslandHost` NSPanel (non-activating, above menu bar, all Spaces,
  click-through compact) with `present(content:)`/`dismiss()` +
  `IslandContent` container protocol + `EvieOrbView` (CAMetalLayer live
  path, no-Metal fallback ring) + SwiftUI `OrbView`/`OrbViewHost` +
  placeholder plan/caption/confirm content.
- **R7 previews:** `docs/orb-previews/` — 23 MP4s (every state × dark/light,
  full tour ×2, side-by-side listening, island expand/collapse ×2) + 18
  stills, all headless. Regenerate: `macos/scripts/orb-render-previews`.
- **R8 owner check:** `OrbPreviewApp` (state picker, I/O sliders, mic ONLY
  after the owner clicks "Use microphone", island + presence buttons, live
  CPU/fps/GPU) + `macos/scripts/orb-preview` + `docs/ORB_OWNER_CHECK.md`.
  Built, never launched, by automation.

Checks: `swift run EvieOrbCheck` → **263 passes, 0 failures**, zero
warnings (envelope, state machine incl. all 72 transitions snap-free,
level DSP vs naive, presence mapping, layout over 5 simulated screens,
host config without showing windows, pacing incl. headless display-link
ticks, renderer pixels).

## 2. Performance (Apple M2, release, 60 s runs)

| Case | Frames | CPU % (≤2%) | GPU mean (≤1.5 ms) |
| --- | --- | --- | --- |
| idle/offline gated | 0 | 0.07–0.14 | n/a (no frames) |
| idle/offline link-stopped | 0 (0 violations) | 0.003–0.004 | n/a |
| listening/speaking 60 fps 120 pt | 3600, exact 60.0 | 0.60–1.36 | 0.17–0.23 |
| thinking/acting 60 fps 120 pt | 3600, exact 60.0 | 1.17–1.51 | 0.19–0.44 |
| needs_confirm 60 fps 120 pt | 3600, exact 60.0 | 0.93–1.02 | 0.10–0.23 |
| all 30 fps 120 pt | 1800, exact 30.0 | 0.42–0.81 | 0.09–0.42 |
| success/error 60 s avg | 67–142 then 0 | 0.11–0.22 | 0.10–0.28 |

Full table + methodology + v1 miss appendix in `docs/ORB_PERFORMANCE.md`.
Disclosed tail: 0–1.1% of 60 fps frames measure 1.5–1.96 ms GPU
(display-scheduling jitter, ≤12% of frame budget, zero dropped frames).

## 3. Previews

`docs/orb-previews/README.md` indexes all 23 clips + 18 stills. Real orb
pixels, real layout geometry, real placeholder views rasterized headless;
only wallpaper/menu-bar/notch are drawn mocks (stated in the README).

## 4. Integration steps (for CLI 2's HUD and the app)

CLI 3 performed NO integration (O2): no edits to the app, Package.swift,
or CLI 2's files. The app owner (or CLI 2) wires it as follows:

1. **Depend:** in `macos/Package.swift`, add
   `.package(path: "Packages/EvieOrb")` and `.product(name: "EvieOrb",
   package: "EvieOrb")` to the EV target. (Their file, their edit.)
2. **Host:** create one `IslandHost()` at startup (main thread); set
   `host.placement` from settings; keep it alive for app lifetime.
3. **Presence → orb:** on each SSE frame from
   `GET /v1/fabric/presence/stream`, call
   `host.handlePresenceEvent(named:panicked:)`. Mapping (also in
   `PresenceMapping`, the single table):
   `turn.listening`→listening, `turn.thinking`→thinking,
   `tool.started`→acting, `turn.needs_confirm`→needsConfirm,
   `turn.completed`→success, `turn.failed`→error,
   `panic.state`→error(idle when cleared); everything else ignored.
   Drive `.offline` from `OfflineDetector` (note every frame/heartbeat;
   `isOffline` past 20 s). Set `.speaking` when TTS starts/stops
   (output-audio truth, not `turn.progress`).
4. **Audio → orb:** in the existing mic tap
   (`LiveConversation.swift`, beside `ingestInputPCM16`) call
   `AtomicOrbLevelMeter.shared.ingestInputPCM16(data)`; in the TTS PCM
   path (`TTSPlayer.swift`) call `ingestOutputPCM16`. Set
   `host.orbView.levelSource = AtomicOrbLevelMeter.shared`. Never a
   second capture.
5. **Content (CLI 2):** implement `IslandContent` (`view`,
   `contentHeight`, `contentID`) for plan steps, captions, confirm card;
   `host.present(content:)` / `host.dismiss()`. Delete the placeholders
   when the real views land (same protocol).
6. **Verify:** `swift run EvieOrbCheck` in `macos/Packages/EvieOrb`;
   owner runs `macos/scripts/orb-preview` for the live check.

## 5. Known gaps (WHAT IS STILL NOT REAL)

- App integration (§4) is documented, not done — by design (O2).
- `OrbPreviewApp` built, never launched (O4) — owner runs it.
- Compact orb sits RIGHT of the notch (setting to choose side not built).
- Twin identical-size screens both hide when one app is fullscreen
  (documented edge in `IslandHost`).
- Display-link dealloc race accepted (views are app-lifetime; noted in code).
- Previews' wallpaper/menu-bar/notch are drawn mocks (stated in README).
- `swift test` cannot run on CLT-only Macs (no XCTest); the repo-idiom
  `EvieOrbCheck` executable is the suite (263 checks, all green).

## 6. Reviewer verdict

Independent reviewer subagent (fresh session, no files modified):

- Scope (O1–O2): PASS — branch `evie-orb`, worktree `../evie-orb`, 76 files,
  out-of-scope grep empty.
- Checks: PASS — `passes: 263 failures: 0`, re-run identical (no flakes).
- Build: PASS — `swift build` + `OrbPreviewApp`, zero warnings, app never
  launched.
- Bench spot-check: PASS — idle 0 frames; listening 60 fps 120 pt 10 s:
  60.0 fps, CPU 0.94%, mean GPU 0.203 ms, spikes 0.
- Previews: PASS — 23 MP4 + READMEs + 18 stills; durations exact; viewed
  island + success frames, content matches.
- Isolation (O3): PASS — `orderFrontRegardless` only behind `showWindow`
  (checks use false); mic only in the owner app behind the click gate.
- Docs: PASS after fix — reviewer found `ORB_PERFORMANCE.md` untracked
  (committed in the final commit below) and one imprecise range
  (corrected: 0.76–1.40%).

**Verdict: READY TO MERGE.** No scope leaks, no flakes, no isolation
violations, no hidden misses.

## 7. Merge command (human runs this, not CLI 3)

```sh
git checkout main && git merge --no-ff evie-orb
```

Scope proof: `git diff --stat main...evie-orb` shows ONLY
`macos/Packages/EvieOrb/`, `macos/scripts/orb-*`, `docs/ORB_*.md`,
`docs/orb-previews/`. No push per O5.
