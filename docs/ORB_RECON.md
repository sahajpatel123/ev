# ORB RECON — Evie Orb + Notch Island (CLI 3)

Date: 2026-10-26. Worktree: `../evie-orb`, branch `evie-orb`, based on `main`
(`bf0412c`, 2026-10-26 18:23 +0530) — newer than `evie-acceptance` (`ec3f764`,
10:47), so `main` is the base. No merge/push/deploy per O5.

## 1. What exists today

- **Existing orb is video-based, not procedural.** `macos/Sources/EV/VoiceOrbRenderer.swift`
  (847 lines) composites an MP4 reference asset through a Metal quad shader
  (`alpha = max(R,G,B)` ridge extraction). There is no procedural shader in
  the tree. This task's original procedural ring does not duplicate or
  conflict with it; integration (which orb the app shows, when) is a
  documented step for later.
- **Audio tap points already exist** (R3 feeds from these, no new capture):
  - Input: `LiveConversation.swift:1338` —
    `VoiceLevelMeter.shared.ingestInputPCM16(data)` (mic PCM16 chunks).
  - Output: `TTSPlayer.swift:1201` —
    `VoiceLevelMeter.shared.ingestOutputPCM16(pcm)` (TTS PCM).
  - Math: `EVRuntime/VoicePresenceMath.swift` — `pcm16RMS`, log-ish
    `normalizeSpeechRMS` (pow 0.55), one-pole `smooth(attack:release:)`.
  - My `OrbLevelSource` mirrors this shape (ingest PCM16 on the audio
    thread, publish one atomic Float, renderer polls per frame) but lives in
    the new module with its own envelope (30 ms attack / 250 ms release,
    noise gate) per spec.
- **Panel precedent:** `PresenceOverlay.swift:303` uses
  `NSPanel([.borderless, .nonactivatingPanel])`, `level = .floating`,
  `collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]`,
  transparent. Island host follows this shape at a higher level
  (above the menu bar) with click-through when compact.
- **Presence contract exists** on branch `evie-presence`:
  `docs/contracts/PRESENCE_EVENTS.md` (416 lines, `evie-presence/1`, SSE GET
  `/v1/fabric/presence/stream`). Event names I map from: `turn.listening`,
  `turn.thinking`, `tool.started`, `turn.needs_confirm`, `turn.completed`,
  `turn.failed`, `panic.state`, plus `stream.hello` (connectivity) and
  `turn.progress` (captions, content only). No `offline` event exists —
  offline is derived locally (stream disconnect + reachability), recorded in
  the mapping table.
- **CLI 2 footprint** (do not touch): `macos/Sources/PresenceGesture/`,
  `macos/Sources/EV/OptionGestureTap.swift`, `macos/PresenceChecks/`,
  `macos/Package.swift` (+14 lines), `docs/PRESENCE_*`. I add zero lines to
  any of these.

## 2. Build facts

- `macos/Package.swift`: swift-tools 6.0, `.macOS(.v14)`, `swiftLanguageModes:
  [.v5]`, links Metal/AVFoundation/QuartzCore already. Toolchain here: Swift
  6.3.2, arm64, Apple M2. `ffmpeg` present (`/opt/homebrew/bin/ffmpeg`).
- Display link: **CVDisplayLink** (correct API for the macOS 14 target; no
  newer AppKit replacement). Notch APIs (`NSScreen.safeAreaInsets`,
  `auxiliaryTopLeftArea/RightArea`) need macOS 12+ — fine on 14.

## 3. Decisions (with justification)

1. **Module = new package `macos/Packages/EvieOrb`** (own `Package.swift`,
   Sources + Tests), NOT a target in `macos/Package.swift`. Reason: O2
   forbids editing shared files and CLI 2 owns `macos/Package.swift` edits;
   a standalone package merges with zero conflicts and can build/test in
   isolation (`swift test` inside the package dir).
2. **Raw Metal, not SwiftUI Shader.** ShaderLibrary exists on macOS 14, but
   the spec needs per-frame uniforms, premultiplied-alpha transparent
   compositing, static-frame caching (0 fps), CVDisplayLink pacing, GPU
   timestamps, and fully headless texture rendering — all first-class in
   raw Metal, all awkward through SwiftUI effects. The benchmark (R5)
   measures octaves/resolution tradeoffs directly on the Metal pipeline.
3. **Original shader, clean-room.** Domain-warped fbm in polar coords around
   an analytic ring SDF, tangential flow, additive glow. No reference GIF
   bytes anywhere; mood only. (CLAUDE.md R15 agrees.)
4. **Pure-logic cores kept AppKit-free** (`OrbStateMachine`,
   `EnvelopeFollower`, `IslandLayout`, presence mapping) so unit tests run
   headless and fast; AppKit/Metal confined to `OrbRenderer`, `FramePacer`,
   `IslandHost`.
5. **Previews via headless harness executable** in the package
   (`EvieOrbPreview`: PNG sequences → ffmpeg GIF/MP4), never on-screen.

## 4. Plan (deliverable order)

1. Package skeleton + `OrbState`/mapping + state machine + envelope +
   level source, with tests first (R2, R3).
2. `orb.metal` + `OrbRenderer` offscreen (R1) + renderer smoke tests.
3. `FramePacer` (R4) + benchmark harness + `ORB_PERFORMANCE.md` (R5),
   optimizing until targets hold.
4. `IslandHost` + `IslandLayout` + layout tests (R6).
5. Preview clips + `orb-preview` script + `ORB_OWNER_CHECK.md` (R7, R8).
6. Reviewer subagent re-run, `ORB_MERGE_READINESS.md`, checkpoint, stop.
