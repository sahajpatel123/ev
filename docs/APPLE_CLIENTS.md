# EV Apple Clients — build, install, permissions, and the iOS path

**Agent 18 (SUIT)** owns this document, `macos/**`, `ios/**`, and
`docs/APPLE_CLIENTS.md`. It is the honest operating manual for the native
Apple clients: what is real today, what needs Xcode, and why every permission
exists.

## 1. Status (read this first)

| Surface | State |
| --- | --- |
| `macos/` menu-bar app (`EV.app`) | **Built and packaged with Command Line Tools only.** `swift build -c release` succeeds; `scripts/package.sh` produces `build/EV.app`; the app launches as an accessory menu-bar process. |
| Shared client (`ios/EVClient`) | Built. `EVClientCheck` and `EVUIValidate` pass under CLT. |
| iOS project (`ios/EV.xcodeproj`) | **Authored and internally consistent, NOT compiled.** There is no Xcode on this machine (Command Line Tools only; owner must free ~20 GB), so nothing under `ios/` has been built, signed, or run. This document gives the exact build path for the day Xcode arrives. |
| Watch complication | Authored as a WatchKit complication data source (`EVWatchExtension/ComplicationController.swift`) that renders the existing `WatchComplicationStub` layouts. Unbuilt (Xcode required). |

No claim is made that the iOS project compiles; that would be a lie until
Xcode runs it.

## 2. Build the macOS menu-bar app without Xcode

Requirements: macOS 14+, Command Line Tools (`xcode-select --install`),
Swift 6.3.2 (shipped with current CLT). No Xcode, no third-party packages.

```sh
cd macos
swift build -c release
./scripts/package.sh
open ./build/EV.app
```

`package.sh`:

1. Builds `EV` and `EVNotificationHelper` with `swift build -c release`.
2. Writes the hand-authored `Resources/Info.plist` into
   `build/EV.app/Contents/Info.plist`.
3. Copies both executables into `Contents/MacOS/`.
4. Ad-hoc codesigns the bundle so launchd/TCC sees a real app identity
   (`codesign --force --deep --sign -`).

The app is an `LSUIElement` accessory: it lives in the menu bar and never
shows a Dock icon. Status glyphs: green listening, blue thinking, purple
speaking, gray offline.

Configuration (environment variable → `defaults` → default):

| Variable | Default |
| --- | --- |
| `EV_API_URL` | `http://127.0.0.1:8000` |
| `EV_API_KEY` | `dev` |
| `EV_DEVICE_ID` | `mac-<hostname>` |

Example:

```sh
EV_API_URL=https://ev.example.com EV_API_KEY=secret ./build/EV.app/Contents/MacOS/EV
```

## 3. Install and launch at login

1. `cp -R macos/build/EV.app /Applications/`
2. `open /Applications/EV.app`
3. In the menu-bar panel, toggle **Launch at login**.
   - `SMAppService.mainApp.register()` requires the app to live in
     `/Applications` (or another approved location); until then the toggle
     shows an error with the reason. Moving the app and toggling again fixes
     it.
4. The app survives logout/login because the login item points at the app
   bundle, not at a random binary.

## 4. Every permission, what it breaks, and the deep link

The Permissions panel inside the app checks each permission and deep-links to
the exact System Settings pane. Silent failure is treated as a bug: every
denied permission produces a visible, actionable message.

| Permission | What breaks if denied | System Settings deep link |
| --- | --- | --- |
| Microphone | Hotkey/Talk cannot record; wake → verify → utterance voice flows become text-only. | `x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone` |
| Screen Recording | Agent 13's ScreenCaptureKit ambient collectors cannot see the screen; HUD remains text/data only. | `x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture` |
| Camera | Photo/video capture events are unavailable (share sheet and text capture still work). | `x-apple.systempreferences:com.apple.preference.security?Privacy_Camera` |
| Notifications | Agent 14's alert/digest delivery cannot surface through the single native path. | `x-apple.systempreferences:com.apple.preference.Notifications` |
| Accessibility | The global hotkey (⇧⌘E) cannot observe other apps' key events, so it will not trigger the mic from anywhere. | `x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility` |

The app never records, captures, or monitors anything until the user invokes
the relevant flow; raw audio is held in memory only for the active utterance
and never persisted by the client.

## 5. Voice and TTS wiring

- Mic capture uses real `AVFoundation`: 16 kHz mono 16-bit PCM, base64-encoded
  at the moment of use.
- Flow: `POST /v1/voice/wake` → (if enrolled) `POST /v1/voice/verify` with the
  challenge nonce/phrase and the captured samples → `POST /v1/voice/utterance`
  with `audio_b64`.
- Agent 4 returns `tts.audio_ref`; the app fetches those bytes with the same
  bearer token and plays them with `AVAudioPlayer`.
- The server-side ears path (`/v1/ears/wake`, Agent 3) is respected as the
  always-on wake pipeline; SUIT does not duplicate it. SUIT only captures
  when the user presses the hotkey/Talk button.

## 6. Notifications — exactly one delivery path

Agent 14's `macos` notifier backend is designed to exec a helper binary.
SUIT supplies that helper (`EVNotificationHelper`) as a SwiftPM target and
packages it inside `EV.app`. The helper:

1. Receives `--id --bundle-id --title --body` from the backend.
2. Opens `ev://notification?...` via the app's registered URL scheme.
3. The **only** component that calls `UNUserNotificationCenter` is the EV app
   itself, so there is one native delivery path with a real bundle identity.

To point Agent 14 at this helper (no backend edits needed):

```dotenv
EV_NOTIFY_MACOS_HELPER_PATH=/Applications/EV.app/Contents/MacOS/EVNotificationHelper
```

Agent 18 requests (dependency note): Agent 14 keeps the helper contract
`--id/--bundle-id/--title/--body`, and does not add a second macOS delivery
mechanism. If the backend wants a delivery receipt, the endpoint to add is an
ack from EV.app (suggested: `POST /v1/notify/{id}/receipt`), not a parallel
notifier.

## 7. iOS project — authored, unbuilt, exact build path

### What is authored

`ios/EV.xcodeproj` contains four targets:

| Target | Bundle id | Purpose |
| --- | --- | --- |
| `EVApp` | `com.ev.ios` | iOS app; `@main` App hosts the shared `AppShellView` from `EVUI`. |
| `EVShareExtension` | `com.ev.ios.share` | Text/URL/file capture from the share sheet. |
| `EVWatchApp` | `com.ev.ios.watchapp` | Watch quick capture + HUD card. |
| `EVWatchExtension` | `com.ev.ios.watchapp.extension` | WatchKit complication data source rendering `ev.hud.card.v1`. |

The project includes:

- `Info.plist` with every usage string (microphone, camera, photos, health,
  location) and `UIBackgroundModes` (`audio`, `fetch`, `processing`,
  `remote-notification`).
- Entitlements: HealthKit read, APNs (`aps-environment`), app group
  `group.com.ev.suit`, keychain-access-group `$(AppIdentifierPrefix)com.ev.ios`.
- Local SwiftPM dependency on `EVClient` (products `EVClient` and `EVUI`).
- Keychain token storage via `KeychainTokenStore` (shared with the extension
  through the access group).
- APNs registration: `registerForRemoteNotifications()` → token upload to
  `POST /v1/devices/{id}/push-token`. That endpoint does not exist yet;
  Agent 14 owns it. Until then the upload fails silently and the app works.
- HealthKit read access (heart rate, HRV, sleep, steps, energy) with no
  write-back.
- Watch complication source replacing `WatchComplicationStub` with a real
  `CLKComplicationDataSource`.

### Exact build path when Xcode arrives

1. Free ~20 GB and install Xcode from the Mac App Store (`xcode-select -p`
   must point at Xcode, not CLT).
2. `open ios/EV.xcodeproj`.
3. Select the `EVApp` scheme and an iOS simulator or your device.
4. Set your development team under Signing & Capabilities (HealthKit and APNs
   need a real team; simulator builds work without a paid account).
5. `Product ▸ Run` (⌘R). The watch app is embedded automatically through the
   "Embed Watch Content" build phase; the share extension through "Embed App
   Extensions".
6. Expected first-fix list (unverified code):
   - Any WatchKit/ClockKit API drift on the installed watchOS SDK.
   - The asset catalog needs real icon PNGs; the authored `Contents.json`
     placeholders are valid but empty.
   - Code-signing profile names under a new team.

Report honestly: until step 1–6 happen, `ios/EV.xcodeproj` is **unbuilt**.

## 8. Validation

```sh
cd macos && swift build -c release && ./scripts/package.sh
cd ../ios/EVClient && swift run EVClientCheck && swift run EVUIValidate
```

`EVClientCheck` now also covers: typed TTS/style decoding, audio-bearing
utterance requests, chat SSE (`delta`/`refined`/`done`/`error`), voice SSE
(`partial`/`final_transcript`/`reply`), and the keychain token store (skipped
if the unsigned CLT binary is denied Keychain access).

### Headless smoke result (2026-08-11, local dev backend)

`macos/.build/release/EV --smoke-test` against a locally started backend
(temporary SQLite in `/tmp`, `EV_VOICEPRINT_PROVIDER=http` pointing at the
included `macos/scripts/mock_embed_server.py`) produced:

```text
health: status=ok app=EV version=0.1.0
chat: text=EV: I heard you. (echo provider — 'Reply with exactly: smoke test ok')
      conversation=3ef898ca-0a58-4200-b288-0f490288374d model=echo-local tokens=265
wake: state=idle session=nil enrolled=false
voice: no session (owner not enrolled) — utterance path skipped
```

The streaming chat leg is proven end to end. The voice verification/utterance
leg is blocked by a real security gate, not by the client: enrollment fails
closed because `AudioLivenessModel` has no weights on this host
(`liveness model unavailable; failing closed (degraded)`). Granting consent
and calling `/v1/voice/enroll` with dummy audio returns exactly that honest
failure. The client's `wake → verify → utterance(audio_b64) → TTS fetch` path
remains unit-tested via `EVClientCheck` and exercised through wake; full
voice E2E requires the owner's liveness model (`liveness-audio`, 2 MB,
registered with ModelArbiter) or a real provider.

### Permission detection evidence (2026-08-11)

`EV --permissions` from the packaged bundle reported:

```text
microphone: granted
camera: granted
screenRecording: granted
notifications: notDetermined
accessibility: denied
  breaks: The global hotkey cannot see key presses from other apps, so ⇧⌘E will
          not open the mic from anywhere.
  settings: x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility
```

Every row includes what breaks and the exact System Settings deep link, and
the command exits 0 once detection itself works. On this host, the only
actionable gap is Accessibility (and a first-time notification grant).

### Notification path evidence (2026-08-11)

- `EV --notify-test` reports `notDetermined — authorization prompt required;
  skipped` (no prompt is shown by an automated probe).
- Cold-start delivery is verified: with EV.app not running,
  `EVNotificationHelper --id … --title EV --body …` exited 0 and the EV.app
  process appeared via the `ev://notification` URL scheme — the single
  delivery path (backend → helper → app → UNUserNotificationCenter) launches
  the app when needed.

### Local audio hardware evidence (2026-08-11)

With Microphone already granted, the bundled app recorded two seconds of real
audio:

```text
mic: recording for 2 seconds
mic: captured 66118 bytes ≈ 2.07s (16 kHz mono PCM)
mic exit=0
```

The playback path (the same `AVAudioPlayer(data:)` call used for Agent 4's
`tts.audio_ref`) was verified with a locally generated tone:

```text
tts: generated 16044 byte WAV
tts: playback started duration=0.50s
tts: playing after 0.3s=true
tts exit=0
```

So the two hardware legs of the voice loop (capture and playback) are now
proven on this host. What remains unproven is the backend voice lifecycle
between them (verify → utterance → TTS audio_ref), which is gated on the
human-supplied `liveness-audio` model.

## 9. Dependency notes raised by SUIT

| # | Owner | Ask |
| --- | --- | --- |
| 1 | Agent 14 | Point `EV_NOTIFY_MACOS_HELPER_PATH` at SUIT's helper; keep the helper arg contract; do not add a second delivery path; consider `POST /v1/notify/{id}/receipt`. |
| 2 | Agent 14 / Agent 1 | Add `POST /v1/devices/{id}/push-token` (additive) for iOS APNs token registration; SUIT's app already sends the exact payload. |
| 3 | Agent 1 | Add `swift build -c release` under `macos/` plus `EVClientCheck`/`EVUIValidate` to CI if the runner has Swift/CLT; macOS runners do. |
| 4 | Agents 6, 12, 13, 14 | Swift helper consolidation: host Vision OCR, ScreenCaptureKit, EventKit, CoreLocation, and notification helpers as SwiftPM targets under `macos/Sources/Helpers/` (see `macos/Sources/Helpers/README.md`) so there is one Swift toolchain story. Needs their agreement on ownership/paths. |
| 5 | Agent 3 | Keep `/v1/ears/wake` as the always-on wake path; SUIT only sends user-triggered utterance audio to `/v1/voice/utterance` (with `audio_b64`). |
