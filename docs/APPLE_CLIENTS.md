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
3. Copies all three executables (`EV`, `EVNotificationHelper`, `EVLifeHelper`)
   into `Contents/MacOS/`.
4. Ad-hoc codesigns the bundle with `Resources/EV.entitlements` (audio-input,
   camera, contacts, location, Apple Events) so launchd/TCC sees a real app
   identity and the LIFE permissions carry the declared entitlements.

The app is an `LSUIElement` accessory: it lives in the menu bar and never
shows a Dock icon. Status glyphs: green listening, blue thinking, purple
speaking, gray offline.

Configuration (environment → `defaults` → `~/Library/Application Support/EV/api.env` → `~/.ev/env` → repo `.env`):

| Variable | Default |
| --- | --- |
| `EV_API_URL` | `http://127.0.0.1:8000` |
| `EV_API_KEY` / `EV_MASTER_KEY` | (required; `"dev"` 401s against a real master key) |
| `EV_DEVICE_ID` | `mac-<hostname>` |

`scripts/package.sh` writes `api.env` and `defaults` so the GUI app does not
need a shell environment. Always-on listening is `ev.ears`; the menu bar
shows **Listening for EVIE** and opens an overlay when you say the name.

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

---

# WAVE LIFE — permission throne and life-access hub

## 10. macOS permission → capability → helper matrix

Every row below is shown live in the EV menu-bar "Grant EVIE my life" panel
(and by `EV --permissions`). A permission is never reported granted unless the
OS API reports it granted.

| Permission | What it unlocks | Helper command | System Settings deep link |
| --- | --- | --- | --- |
| Microphone | Voice wake/verify/utterance capture | (EV app mic path) | `...?Privacy_Microphone` |
| Speech Recognition | On-device dictation capture | (EV app dictation path) | `...?Privacy_SpeechRecognition` |
| Camera | Photo/video notes | — | `...?Privacy_Camera` |
| Screen Recording | ScreenCaptureKit ambient context (Agent 13) | — | `...?Privacy_ScreenCapture` |
| Accessibility | Global hotkey + System Events UI control | `apps.activate` (via System Events where needed) | `...?Privacy_Accessibility` |
| Automation | Control Messages/Mail/System Events via Apple Events (per-target; panel shows `partial` until every target is granted) | `messages.send`, `mail.list`, `mail.send` | `...?Privacy_Automation` |
| Full Disk Access | Read Messages DB / Mail data | `messages.list`, `mail.list` (DB path) | `...?Privacy_AllFiles` |
| Contacts | Resolve names → phone/email | `contacts.list`, `contacts.resolve` | `...?Privacy_Contacts` |
| Calendars | Calendar events for HUD/routines (Agent 12) | — | `...?Privacy_Calendars` |
| Reminders | Reminder reads/writes for routines | — | `...?Privacy_Reminders` |
| Notifications | Alerts/digests via Agent 14 single path | `EVNotificationHelper` | `com.apple.preference.Notifications` |
| Bluetooth | Paired peripherals (keyboard/mouse/health belt) | — | `...?Privacy_Bluetooth` |
| Input Monitoring | Low-level keyboard/mouse event monitoring | (hotkey fallback) | `...?Privacy_ListenEvent` |
| Location | Location-aware HUD cards | — | `...?Privacy_LocationServices` |

Platform truth: Full Disk Access has no public TCC query API, so EV's live
status uses a conservative probe — readable `~/Library/Messages` and/or
`~/Library/Mail`. Automation is per-target (Messages, Mail, System Events);
the panel reports granted only when every target is granted.

### Why a pane does not list EV, and the fix

macOS registers an app in a privacy pane only **after the app has actually
asked for that permission** (the first TCC prompt or, for Screen Recording /
Input Monitoring, the first registration call). Usage strings in `Info.plist`
are necessary but not sufficient — until the request fires, the pane is empty.

- **Grant All** in the panel (or `EV --request-all`) fires every programmatic
  request in one paced sequence: mic, speech, camera, screen recording,
  contacts, calendars, reminders, notifications, input monitoring, Bluetooth,
  location, then per-target Automation. Each answered prompt registers EV in
  its pane.
- **Per-row Ask** fires one undecided permission. After a denial macOS will
  not re-prompt; only Screen Recording re-registers on request.
- **Accessibility and Full Disk Access** have no request API — add EV with the
  "+" button in those panes.
- The panel auto-runs a one-time (per app version) sweep of still-undecided
  permissions the first time it opens; re-run `EV --request-pending` to fill
  in any pane that is still empty.
- **Grants are tied to the code signature.** An ad-hoc-signed build changes
  its CDHash on every rebuild, so macOS forgets every grant. `package.sh`
  requires the stable `EV Code Signing` identity (`scripts/signing.sh`) and
  refuses to fall back to ad-hoc. Run the packaged app — never `swift run`,
  which executes an unsigned binary that TCC keys differently.

## 11. EVLifeHelper — JSON contract for CONDUIT/CORTEX

### Invocation contract

`EV_LIFE_HELPER_PATH` points at the helper binary. Resolution order:

1. `$EV_LIFE_HELPER_PATH` if set and executable.
2. The helper packaged inside the app:
   `/Applications/EV.app/Contents/MacOS/EVLifeHelper` (or `macos/build/EV.app/...`).

CONDUIT/CORTEX (backend agents) exec `EVLifeHelper <command> [args]` and read
one JSON object from stdout. Exit codes are stable:

| Code | Meaning |
| --- | --- |
| 0 | ok |
| 3 | permission denied (TCC not granted — never fake success) |
| 4 | not available (OS/app/feature missing) |
| 5 | bad arguments |
| 1 | generic failure |

Output shape:

```json
{"ok":true,"data":{...}}
{"ok":false,"error":{"code":"permission_denied","message":"..."}}
```

### Commands

| Command | Args | Real path |
| --- | --- | --- |
| `contacts.list` | — | Contacts framework; requires Contacts permission |
| `contacts.resolve` | `--query <name\|phone\|email>` | Contacts framework, top 10 matches |
| `messages.list` | `--limit N` (default 20) | sqlite3 over `~/Library/Messages/chat.db`; requires Full Disk Access |
| `messages.send` | `--to <buddy> --text <message>` | AppleScript → Messages (iMessage); requires Automation |
| `mail.list` | `--limit N` (default 10) | AppleScript → Mail inbox; requires Automation |
| `mail.send` | `--to <email> --subject <s> --body <b>` | AppleScript → Mail outgoing message + send; requires Automation |
| `call.place` | `--destination <number> [--kind tel\|facetime]` | `NSWorkspace.open` of `tel://` / `facetime://` |
| `apps.frontmost` | — | `NSWorkspace.frontmostApplication` (no permission) |
| `apps.activate` | `--bundle-id <id>` | `NSRunningApplication.activate` / launch |

### Verified on this Mac (2026-08-12)

- `EVLifeHelper` (no args) → exit 5, JSON `bad_arguments`.
- `apps.frontmost` → exit 0, returned Safari.
- `apps.activate --bundle-id com.apple.Finder` → exit 0.
- `apps.activate --bundle-id com.apple.MobileSMS` → exit 0 (Messages opened).
- `messages.list --limit 2` → exit 0, returned real Messages rows with correct
  ISO dates (Full Disk Access is granted on this host).
- After the owner granted Contacts through the in-app request flow,
  `contacts.resolve --query "Jio"` → exit 0, returned real matches with phone
  numbers (e.g. "Jio Fiber", "Jio number(Arshad)").
- `mail.list --limit 1` → exit 0, returned a real inbox message from Mail
  (Mail's per-target Automation grant is live; the aggregate Automation row
  honestly shows `partial` until Messages/System Events are also granted).
- `messages.send --dry-run` → exit 3 `permission_denied` (Messages Automation
  not granted; dry-run aborts before any send).
- `mail.send --dry-run --to … --subject … --body …` → exit 0 with
  `{"dry_run":true,"compiled":true}` (Mail Automation granted; no mail sent).
- `call.check --kind tel` → exit 0, handler `Phone.app`; `call.check --kind
  facetime` → exit 0, handler `FaceTime.app`. No call was placed; this proves
  the schemes `call.place` would open are available.

This satisfies "at least one real Mac path works end-to-end when permissions
are granted": Contacts resolution + opening Messages both work, and
`messages.list` / `mail.list` read real data. `messages.send` becomes live the
moment Messages/System Events Automation is granted in System Settings.

The grant panel also auto-presents on first launch (dismissing it records the
visit), so the owner walks the matrix once instead of hunting for it.

## 12. iOS — both phones, same agency

### Authored (unbuilt until Xcode)

- `ios/EV.xcodeproj` now includes the "Grant EVIE access" screen
  (`EVApp/LifeAccess.swift`) with live status and request buttons for
  contacts, microphone, speech, camera, notifications, location, and
  Bluetooth; the toolbar button on the app shell opens it.
- Info.plist usage strings: contacts, speech, Bluetooth, local network,
  Siri, plus the existing microphone/camera/photos/health/location strings.
- Background modes: `audio`, `voip`, `fetch`, `processing`,
  `remote-notification`.
- Entitlements: HealthKit read, Siri, APNs, app group, keychain access group.
- EVClient life APIs: `postPermissionReport` (→ `POST
  /v1/devices/{id}/permissions`), `EVContactResolver`, `EVMessageURLs` /
  `EVMessageComposer` (MessageUI where available), `EVCallPlacer`
  (`tel:`/`facetime:`), `EVNotificationInbox.summary()`.
- App Intents: `CaptureWithEVIntent` + `CaptureShortcuts` ("Capture with EV")
  for Siri/Shortcuts; authored under `ios/EVApp/AppIntents.swift`.
- CallKit: `EVCallKitManager` reports outgoing calls for the `voip`-capable
  app; authored under `ios/EVApp/CallKitManager.swift`.
- URL schemes available to the app: `sms:`, `facetime:`, `tel:` (no special
  entitlement; the OS prompts on use).

### Exact build/sign/install for two physical iPhones

1. Install Xcode (~20 GB) on the Mac; `xcode-select -p` must point at Xcode.
2. `open ios/EV.xcodeproj`; select the `EVApp` scheme.
3. In Signing & Capabilities, choose your Apple Developer team (free personal
   teams work for contacts/mic/speech/camera; APNs and HealthKit need a paid
   team for on-device builds).
4. Change the bundle identifiers if needed: `com.ev.ios` (phone 1) and
   `com.ev.ios.2` (phone 2) — or keep one id and use two devices on the same
   team.
5. Register both iPhones in the Apple Developer portal / Xcode Devices window
   (UDID from `Settings → General → About`).
6. Enable required capabilities in the project editor: HealthKit, Push
   Notifications, Siri, Background Modes (voip/audio/fetch/processing),
   App Groups, Keychain Sharing.
7. Create the app group `group.com.ev.suit` and keychain access group
   `$(AppIdentifierPrefix)com.ev.ios` in both the app and share extension.
8. Install Tailscale on both phones and the Mac; keep them on the same tailnet
   so the phone can reach the Mac API at the Mac's Tailscale IP.
9. In `EVApp/Info.plist`, set `EV_API_URL` to
   `http://<mac-tailscale-ip>:8000` (or set it at runtime via
   `UserDefaults.standard`).
10. Connect each iPhone, select it as the run destination, and ⌘R.
11. First launch: the "Grant EVIE access" toolbar button walks each
    permission; denied permissions show their exact consequence.
12. APNs token registration uploads to `POST /v1/devices/{id}/push-token`
    once Agent 14 lands that endpoint; until then the app works without push.

Honest status: nothing in `ios/` has been compiled or run. Xcode is not
installed on this machine. The project is authored and internally consistent;
the owner only needs Xcode + team + two registered devices to execute the
steps above.

## 13. VERIFY

```sh
cd macos
swift build -c release
./scripts/package.sh
./build/EV.app/Contents/MacOS/EV --permissions
./build/EV.app/Contents/MacOS/EVLifeHelper apps.frontmost
./build/EV.app/Contents/MacOS/EVLifeHelper messages.list --limit 2
cd ../ios/EVClient
swift run EVClientCheck
swift run EVUIValidate
```

Offline CI never needs Apple TCC: EVClientCheck runs with mocked HTTP and
skips keychain when the unsigned CLT binary is denied; EVLifeHelper's
permission-gated commands return their documented exit codes without prompts.

---

# EVIE opens a window

## 14. Product surface

The product surface is **"EVIE opens a window"**, not "open the EV app and
browse a dashboard". EVIE summons a contextual floating overlay when she has
something to show; the owner never navigates to a screen for it.

- The **menu bar extra is the only persistent chrome**: status, Talk, and
  "Last overlay". Capture and chat remain available inside the menu-bar panel,
  but all copy says **EVIE**.
- The **permission matrix appears only during the initial grant flow** (it
  auto-presents on first launch and is reachable from the panel afterwards;
  it is not a persistent settings surface).
- Every summoned surface uses the same visual language: translucent dark glass
  (window opacity 75%, pane tint 30% so 70% of the desktop shows through),
  cyan EVIE wordmark, warm gold JARVIS accents, one typography hierarchy,
  Escape/close dismissal, and content-first layout.

## 15. `ev://present` kinds, sizes, and time-types

Intelligence (`app.ev.lookout.plan_surfaces`) decides whether to open glass,
how many windows, and which catalog entries to use. JARVIS-style sizes and
Karen-style time-types are independent of kind. Lookouts persist in a corner
until dismissed; flashes die on their own.

### Kinds

| Kind | What it is | Default size | Default time | Default place |
| --- | --- | --- | --- | --- |
| `card` | Status / one answer | card 480×300 | linger 30s | center |
| `briefing` | Tactical brief | brief 560×420 | linger | center |
| `list` | Priorities / checklist | slate 720×520 | hold | center |
| `conversation` | Live thread | brief | hold | center |
| `map` | Route / location | canvas 960×680 | hold | center |
| `chip` | Instant confirmation | chip 280×148 | flash 1.6s | upper_right |
| `radar` | Alert / deadline lookout | lookout 340×460 | lookout | upper_right |
| `vitals` | Body-scan / readiness | lookout | lookout | upper_left |
| `horizon` | Next commitment / day | lookout | lookout | lower_right |
| `scope` | Person / focus lock | lookout | lookout | right |
| `bench` | Ops / gear | lookout | lookout | left |
| `trace` | Research / audit | slate | hold | center |
| `pulse` | Urgent countdown | chip | pulse | top |
| `ticker` | One-line bar | ticker 920×64 | glance 5s | top |
| `wire` | Live voice session | lookout | session | lower_left |

Sizes: `pip` `chip` `card` `brief` `slate` `canvas` `lookout` `ticker`.
Time-types: `flash` `glance` `linger` `hold` `lookout` `pulse` `session`.

Query parameters:

- `title`, `body`, `kind` (default `card`; `auto` lets intelligence choose)
- `size`, `time`, `place`, `ttl` (milliseconds), `id`, `lookout=1`
- `items` — pipe (`|`) or newline separated entries
- `recommendation`, `source`
- `lat` / `lon` / `dest_lat` / `dest_lon` — map route coordinates

`ev://dismiss?id=` hides one window. `ev://dismiss-all` (or `ev://dismiss`
without an id) hides every overlay.

When the native app is not the `ev://` handler, EVIE may open independent
`/app/lookout` or `/app/stage` visor windows instead of the workbench. That
is a fallback, not the product.

## 16. macOS NSPanel behaviour

Each lookout is its own `NSPanel` (`.titled`, `.closable`,
`.fullSizeContentView`, `.nonactivatingPanel`). The panel is not opaque:
`isOpaque = false`, `backgroundColor = .clear`, `alphaValue = 0.75`, and
the SwiftUI pane fill is a 30% tint so 70% of the desktop stays visible.
Several can be on screen at once, parked in the corners like JARVIS
peripheral feeds. Floating level; `canJoinAllSpaces` +
`fullScreenAuxiliary`; movable by background; Escape / close / TTL
dismissal. Timed windows (`flash`, `glance`, `linger`, `pulse`) close
themselves. Lookouts stay until the owner or EVIE dismisses them.

Accessory-mode presentation path (explicitly tested):

1. EV runs as `LSUIElement` / `.accessory`.
2. `ev://present` arrives through the `ev://` URL scheme.
3. `PresenceController` activates the app, `makeKeyAndOrderFront`, then
   `orderFrontRegardless` — the panel becomes visible and floats above normal
   windows (`CGWindowLayer == 3` on-screen).
4. `ev://dismiss` (or Escape/close) hides it.

## 17. iOS sheet equivalent

iOS will present the same five kinds as **sheets summoned by EVIE** (deep link
or App Intent), never as a 15-tab application. The architecture maps directly:

| macOS | iOS |
| --- | --- |
| `NSPanel` overlay | `.sheet` on the root scene |
| `PresenceKind` | `PresenceKind` (shared enum direction) |
| `ev://present?kind=…` | `ev://present?kind=…` handled by the app |
| MapKit `Map` | same SwiftUI `Map` |

The iOS project already owns the URL-scheme-ready app shell; the sheet
presenter is a future iOS target that reuses the same `PresenceContent` model.

## 18. Verification (2026-08-13)

```sh
cd macos && swift build -c release && ./scripts/package.sh
open ./build/EV.app
open "ev://present?kind=card&title=Card%20Title&body=…"
swift macos/scripts/window_probe.swift
```

Results:

```text
card:         480×300 on screen, layer 3, dismissed via ev://dismiss
briefing:     560×420 on screen, layer 3, dismissed
list:         460×380 on screen, layer 3, dismissed
conversation: 560×420 on screen, layer 3, dismissed
map:          640×460 on screen, layer 3, dismissed (coords + empty state)
```

`package.sh` PASS; `--smoke-test` PASS against a local backend (health + SSE
chat + voice wake with a local dummy audio_ref).
