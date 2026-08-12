# EV iOS / watchOS clients

`EVClient` is the shared Swift package both the iPhone and Watch apps build on.
It is a thin, authenticated viewer/capturer of the same EV backend as the web
and CLI clients — one backend, one memory, one sync contract.

## What it provides

- **`EVAPIClient`** — async/await v1 API client: capture (with
  `Idempotency-Key`), attachment upload (photos/files via multipart), ask,
  timeline, memories, audit, HUD card, health. Bearer token auth, snake_case ↔
  camelCase mapping, ISO-8601 timestamps kept as strings for faithful
  rendering.
- **`HUDCard`** — `ev.hud.card.v1` model with schema validation and a compact
  `renderText()` shared by Watch complications, widgets, and voice one-liners.
  Same output shape as the CLI `ev card` and web workbench HUD panel.
- **`OfflineCaptureQueue`** — offline-first capture queue. Pending captures
  persist with idempotency keys; `sync(using:)` applies the same contract as
  `ev sync` and the web client: 201 synced, 409 duplicate dropped, 422
  quarantined, network failure preserves the queue. Attachment captures
  (photos/files) queue with their local file path and upload as multipart on
  sync; records whose file has disappeared are quarantined.
- **`EVUI`** — shared SwiftUI views (`TodayView` with HUD card, `CaptureView`
  with offline queue fallback, `MemoryBrowserView` with audit drill-down) that
  compile on macOS and are imported by the iPhone/Watch app targets. The
  `AppShellView` tab shell adds one continuous `ConversationView` (default
  thread), a `VoiceCaptureView` wake button (`/v1/voice/wake`), and a
  `QueueIndicatorView`. The voice client covers the full session flow —
  `wakeVoice` → `verifyVoice` → `utterance` — so the shell can wake, verify,
  and speak (text fallback) today; real mic capture requires the iOS app
  target and a permission grant. `WatchComplicationStub` renders HUD card /
  quick-card payloads as complication layouts (title + ≤2 lines) until the
  WatchKit target ships.

## Module map for the app targets

```text
EVApp
├── Capture       (voice, camera, share, notes)  -> OfflineCaptureQueue + EVAPIClient
├── Chat          (streaming + provenance)       -> EVAPIClient.ask
├── Memory        (browser, timeline, audit)     -> EVAPIClient.memories/timeline/audit
├── Today         (dashboard, HUD card)          -> EVAPIClient.hudCard + HUDCard.renderText
└── WatchCompanion (quick capture + HUD cards)   -> same EVClient package
```

See `docs/CLIENTS.md` in the repo root for the full client architecture and
sync protocol.

## Xcode project (authored, not compiled)

`EV.xcodeproj` in this directory defines four targets:

- **EVApp** (`com.ev.ios`) — iOS app whose `@main` App hosts the shared
  `AppShellView` (Today / Chat / Capture / Memory / Voice tabs).
- **EVShareExtension** (`com.ev.ios.share`) — share-sheet capture of text,
  URLs, and files into EV events/attachments.
- **EVWatchApp** (`com.ev.ios.watchapp`) — quick capture + HUD card.
- **EVWatchExtension** (`com.ev.ios.watchapp.extension`) — WatchKit
  complication data source rendering `ev.hud.card.v1` via
  `WatchComplicationStub`.

The project includes full usage strings, background modes, HealthKit read
entitlement, APNs registration (wired to the inert `POST
/v1/devices/{id}/push-token` endpoint owned by Agent 14), keychain token
storage shared with the extension, and the local `EVClient` SwiftPM
dependency.

**There is no Xcode on this machine.** The project is authored to be
internally consistent (pbxproj parses, references resolve, plists lint, Swift
sources syntax-parse) but has **not** been compiled, signed, or run. See
`docs/APPLE_CLIENTS.md` §7 for the exact build path and disk requirement.

## Streaming

`EVAPIClient` now exposes:

- `askStream(...)` — SSE chat (`memory-delta`, `provenance`, `filter-report`,
  `context-plan`, `delta`, `refined`, `done`, `error`).
- `streamUtterance(...)` — SSE voice (`partial`, `final_transcript`, `reply`,
  `error`, `done`).
- `utterance(..., audioB64:)` — voice requests with captured PCM.
- `KeychainTokenStore` and `EVClientAppConfig` for shared app/extension/watch
  configuration.

`EVClientCheck` verifies all of the above with a mocked transport.

## Validate

```sh
cd EVClient && swift run EVClientCheck
```

`EVClientCheck` is a headless assertion harness (no XCTest dependency, so it
runs with Command Line Tools alone). It verifies HUD decode/validate/render,
idempotent capture (201/409), and offline-queue sync/drop/quarantine/preserve
behavior against a mocked HTTP transport.

`EVUIValidate` builds the shared SwiftUI views on macOS so the UI layer is
compile-checked before it is wired into the iOS/Watch app targets:

```sh
swift run EVUIValidate
```
