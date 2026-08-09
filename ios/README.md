# EV iOS / watchOS client foundation

`EVClient` is the shared Swift package both the iPhone and Watch apps build on.
It is a thin, authenticated viewer/capturer of the same EV backend as the web
and CLI clients — one backend, one memory, one sync contract.

## What it provides

- **`EVAPIClient`** — async/await v1 API client: capture (with
  `Idempotency-Key`), ask, timeline, memories, audit, HUD card, health. Bearer
  token auth, snake_case ↔ camelCase mapping, ISO-8601 timestamps kept as
  strings for faithful rendering.
- **`HUDCard`** — `ev.hud.card.v1` model with schema validation and a compact
  `renderText()` shared by Watch complications, widgets, and voice one-liners.
  Same output shape as the CLI `ev card` and web workbench HUD panel.
- **`OfflineCaptureQueue`** — offline-first capture queue. Pending captures
  persist with idempotency keys; `sync(using:)` applies the same contract as
  `ev sync` and the web client: 201 synced, 409 duplicate dropped, 422
  quarantined, network failure preserves the queue.
- **`EVUI`** — shared SwiftUI views (`TodayView` with HUD card, `CaptureView`
  with offline queue fallback, `MemoryBrowserView` with audit drill-down) that
  compile on macOS and are imported by the iPhone/Watch app targets.

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
