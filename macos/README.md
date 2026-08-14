# EV — native macOS menu-bar app (SUIT)

Builds with Command Line Tools only — no Xcode required:

```sh
swift build -c release
./scripts/package.sh
open ./build/EV.app
```

The packaged app is an `LSUIElement` accessory app: it lives in the menu bar
with a status glyph (Listening for EVIE / thinking / speaking / offline).
Always-on wake is `ev.ears` (launchd) — say **EVIE** and the overlay opens.
Push-to-talk in the panel is a fallback, not the primary way to talk.

Configuration is locked in `Sources/EVAuth/APIAuthKey.swift` (do not invert):
environment → `EV_MASTER_KEY` on disk → `EV_API_KEY` on disk → `defaults`.
Short leftovers (`dev`, `changeme`, keys under 16 chars, `EV_EARS_API_KEY`)
are rejected so the app does not 401 as "Invalid or revoked device token".

- `EV_API_URL` (default `http://127.0.0.1:8000`)
- `EV_MASTER_KEY` (preferred) or a ≥16-character `EV_API_KEY`
- `EV_DEVICE_ID` (default `mac-<hostname>`)

`scripts/package.sh` copies those credentials into Application Support so the
GUI app authenticates even though it is not launched from the repo shell.

The notification helper (`EVNotificationHelper`) is the shim Agent 14's PULSE
backend calls; it forwards to the running EV.app via the `ev://` URL scheme so
there is exactly one UNUserNotificationCenter delivery path.

## EVIE opens a window

EVIE summons floating overlays over any app. Each pane is translucent glass
(75% window opacity, 30% tint — 70% of the desktop shows through):

```sh
open "ev://present?kind=card&title=Status&body=All%20quiet"
open "ev://present?kind=briefing&title=Brief&recommendation=Do%20this&items=A|B"
open "ev://present?kind=radar&size=lookout&time=lookout&place=upper_right&lookout=1&title=Radar&body=Watching%20deadlines"
open "ev://present?kind=vitals&place=upper_left&lookout=1&title=Vitals&body=Readiness%2068"
open "ev://present?kind=chip&time=flash&ttl=1600&title=Got%20it&body=Saved"
open "ev://present?kind=map&title=Route&lat=23.0&lon=72.5&dest_lat=23.1&dest_lon=72.6"
open "ev://dismiss-all"
```

Kinds include cards, briefings, maps, and persistent lookouts (`radar`,
`vitals`, `horizon`, `scope`, `bench`, `wire`). Sizes and time-types are
independent: a briefing can flash as a chip, a radar can linger as a
lookout. Several panels can be on screen at once. Intelligence chooses
the set via `POST /v1/runtime/lookouts`. Verify with:

```sh
swift macos/scripts/window_probe.swift
```

## Headless smoke test

With a backend running locally, the app binary can prove the client wiring
without a GUI:

```sh
./scripts/smoke.sh
```

It exercises `/v1/health`, streaming chat SSE (`/v1/chat`), voice wake
(`/v1/voice/wake`), and — when a voiceprint enrollment exists — voice
verification, utterance, and TTS audio fetch. The voice verification leg needs
a real liveness model on the backend; without one the smoke reports the wake
path and honestly skips enrollment-dependent steps.

Two system-only probes run without a backend:

```sh
./build/EV.app/Contents/MacOS/EV --permissions   # detection + deep links
./build/EV.app/Contents/MacOS/EV --request-all   # fire every programmatic TCC request
./build/EV.app/Contents/MacOS/EV --request-pending  # register EV in still-undecided panes
./build/EV.app/Contents/MacOS/EV --notify-test   # notification auth state
./build/EV.app/Contents/MacOS/EV --mic-test      # 2s real mic capture probe
./build/EV.app/Contents/MacOS/EV --tts-test      # AVAudioPlayer output probe
./build/EV.app/Contents/MacOS/EV --life-request --permission contacts  # TCC request probe
```

## Permissions — why EV is missing from a System Settings pane

macOS only lists an app in a privacy pane **after the app has actually asked
for that permission**. Installing the app and shipping `Info.plist` usage
strings is not enough: until EV calls the matching API (mic capture,
`CNContactStore`, `EKEventStore`, `SFSpeechRecognizer`, …), the pane is empty
no matter how long it sits in `/Applications`.

The Permissions panel ("Grant EVIE my life") fixes this two ways:

1. **Grant All** fires every programmatic request in sequence — one macOS
   consent dialog per permission, paced so none get dropped. Each answered
   prompt registers EV in the corresponding pane. Screen Recording and Input
   Monitoring register without a dialog (they are toggles), and Accessibility
   and Full Disk Access have no request at all — add EV with the "+" button in
   those two panes.
2. **Per-row "Ask"** re-fires a single undecided permission. Once a permission
   is denied, macOS will not prompt again; the row then only offers "Open
   System Settings".

The first time the panel opens, EV automatically runs a one-time (per app
version) sweep of the *still-undecided* permissions so every pane lists EV.
Re-run `--request-pending` (or reopen the panel) any time a pane is missing —
it only asks about permissions macOS still reports as undecided.

### Grants are tied to the code signature

TCC ties every grant to the app's code signature. With an ad-hoc signature the
CDHash changes on every rebuild, so macOS treats each rebuild as a brand-new
app and **forgets every granted permission**. `package.sh` therefore requires
the stable self-signed `EV Code Signing` identity (created by `signing.sh`)
and fails loudly instead of silently falling back to ad-hoc. Keep the same
build installed while toggling panes, and re-run `package.sh` (not `swift run`,
which runs an unsigned binary) for the grants to survive.

### Step-by-step

```sh
./scripts/package.sh                  # requires the stable signing identity
open ./build/EV.app
# Panel → "Grant All" → answer each prompt
# Accessibility + Full Disk Access: add EV with the "+" button
# Screen Recording + Input Monitoring: toggle EV in their panes
./build/EV.app/Contents/MacOS/EV --permissions   # confirm every row is green
```

## EVLifeHelper

The packaged app ships `EVLifeHelper` at
`EV.app/Contents/MacOS/EVLifeHelper` (also built at
`.build/release/EVLifeHelper`). It exposes a JSON stdout contract for
CONDUIT/CORTEX:

```sh
EVLifeHelper contacts.resolve --query "John"
EVLifeHelper messages.list --limit 10
EVLifeHelper messages.send --to "+15551234567" --text "on my way"
EVLifeHelper mail.send --to "a@example.com" --subject "Hi" --body "Body"
EVLifeHelper call.place --destination "+15551234567" --kind tel
EVLifeHelper call.check --destination "+15551234567" --kind tel
EVLifeHelper apps.frontmost
EVLifeHelper apps.activate --bundle-id com.apple.MobileSMS
```

Exit codes: `0` ok, `3` permission denied, `4` not available, `5` bad
arguments. `messages.send` and `mail.send` accept `--dry-run` (compile +
Automation permission check, no side effects); `call.check` verifies the
scheme handler without placing a call. Set `EV_LIFE_HELPER_PATH` to point
CONDUIT/CORTEX at the binary; the fallback is the bundled path.
