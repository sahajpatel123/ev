# iPhone microphone: shell-first architecture (EvieShell)

Status: implemented, awaiting physical-device install (2026-10-08, build
`2026.10.08.4`). iPhone-only; the Mac path is untouched.

## 1. The bug

On the iPhone, tapping Talk shows **"Connecting microphone…"** and the mic
never starts — so spoken commands ("call someone") can never happen.

## 2. Decisive finding: the failing surface is not EvieShell

Server-side proof that the session failing at 13:58 was the **Safari
Home Screen web app**, not the EvieShell app:

| Hello field | Measured | Shell build would send |
| --- | --- | --- |
| `permissions` | `{}` | microphone/camera/… evidence |
| `hardware.model` | `null` | `iPhone17,1` etc. |
| `capabilities` | `["foreground_voice","camera","text","notification"]` | + `microphone`, `location`, `clipboard` |

Those are exactly the PWA's *no-shell* values (`nativeSnapshot()`), so
`window.EvieNativeShell` is absent: the bridge I added can never run on this
surface. Every prior fix — web hardening and native bridge alike — was
applied to a surface the owner is not using.

Corroborating log trail (all PWA taps, every build):

| Build | Beacons received | Next requests |
| --- | --- | --- |
| `2026.10.07.2` | `tap_entry` only | `live/close`, `conversation/release` |
| `2026.10.08.1` | `tap_entry` only | same |
| `2026.10.08.2` | `tap_entry` only | same |
| `2026.10.08.3` | `tap_entry` only | same |

`.08.2` guarantees a `tap_gum_hang` beacon within 6 s even when
`getUserMedia` never settles; it still never arrived. On the iOS Home Screen
web-app surface, WebKit stalls the capture promise and the page's timers stop
being observable — a platform dead-end, not a code path we can patch.

Control facts: voice worked on this phone until 2026-09-16 (Gemini Live
`final_transcript` events), then stopped; the Mac voice path is healthy; the
server and Tailscale Serve are healthy; the phone never calls `/v1/voice/*`.

## 3. Architecture decision (iPhone only)

1. **EvieShell is the voice surface.** The app already has the native
   AVAudioEngine capture bridge (previous section, now verified to be
   absent from the failing sessions) plus the WKWebView mic-permission
   delegate. Voice in Safari web apps is not reliable by platform design, so
   the Home Screen PWA gets an honest notice rather than a silent stall.
2. **The shell must be installable and self-configuring.** A direct Xcode
   install used to bundle `http://127.0.0.1:8000`, which cannot work from a
   phone; there was no on-device way to fix it. Now:
   - `Info.plist` carries `$(EV_API_URL)`; the build setting defaults to
     loopback and the release pipeline / install script override it.
   - First launch shows a `HomeStationSetupView` when no Home Station origin
     is known, with validation; saving re-loads the web core.
   - `scripts/ios/install-evie-iphone.sh` builds for a USB-connected iPhone
     with the Tailscale HTTPS origin auto-detected, installs, and launches —
     no `defaults write`, no OTA round trip.
3. **The PWA tells the truth about its surface.** `iosVoiceSurface()` labels
   `shell` / `safari-tab` / `home-screen`; Home Screen taps get a caption
   pointing at Safari or the app before capture starts; a hang is reported
   as a hang, not as "access denied". Extra bisect beacons (`tap_mood_set`,
   `tap_media_prime`) close the remaining blind spot in field logs.

## 4. Files changed (this round)

| File | Change |
| --- | --- |
| `ios/EvieShell/App/EvieShellApp.swift` | `@AppStorage("evie.api_origin")` gate + `HomeStationSetupView` |
| `ios/EvieShell/App/WebCoreContainer.swift` | `isHomeStationOrigin`, `needsSetup` |
| `ios/EvieShell/App/Info.plist` | `EV_API_URL = $(EV_API_URL)` (build-setting substitution) |
| `ios/EvieShell/EvieShell.xcodeproj/project.pbxproj` | `EV_API_URL` default in Debug + Release |
| `scripts/ios/install-evie-iphone.sh` | one-command device build/install/launch |
| `backend/clients/pwa/app.js` | `iosVoiceSurface()`, Home Screen caption, truthful hang/denial copy, bisect beacons, diagnostics row |
| `backend/tests/test_pwa_audio.py` | surface + shell-setup regression tests |
| build stamps `.4` + `release.json` | regenerated |

Carried over from the previous round (still required for shell voice):
`NativeMicCapture.swift`, `CapabilityBroker` mic handlers,
`Contract.allowedTypes`, pbxproj registration, `attachNativeCapture` loop.

## 5. Owner steps

1. **Right now (proves the diagnosis):** open
   `https://sahajs-macbook-air.tailbd71d0.ts.net/evie/` in a **Safari tab**
   (type the address; do not tap the Home Screen icon) and tap Talk.
   A Safari tab can prompt for the mic; the Home Screen web app cannot
   reliably. If this works, phone voice works today.
2. **Install the real voice app:** connect the iPhone to this Mac with a
   cable, unlock it, tap Trust, then run:
   `scripts/ios/install-evie-iphone.sh`
   (auto-detects `https://sahajs-macbook-air.tailbd71d0.ts.net`, stamps it
   into the app, builds, installs, launches). If Xcode asks for a signing
   team, set it once in the project or pass `DEVELOPMENT_TEAM=…`.
3. Tap Talk in the **Evie app**; expect beacons `tap_native_mic` →
   `tap_native_ok` → `pcm_listen` and the iOS microphone prompt on first use.

## 6. Verification on this tree

| Check | Result |
| --- | --- |
| `xcodebuild -scheme Evie -sdk iphonesimulator build` | BUILD SUCCEEDED; built Info.plist shows the substituted `EV_API_URL` |
| `swift run --package-path ios/EvieShell EvieBrokerCheck` | all checks PASS |
| `pytest tests/test_pwa_audio.py tests/test_release_contract.py` (from `backend`) | 21 passed |
| `node --check app.js` + PWA node tests | OK |
| `.venv/bin/python -m ruff check app clients tests` | clean |

## 7. WHAT IS STILL NOT REAL

- No physical iPhone has run the shell build yet — the Mac had no device
  attached (`devicectl` showed only a simulator). Device audio is unverified
  until step 2 above.
- The Safari-tab result is unknown; if a Safari tab also stalls, the Home
  Screen/WebKit finding stands and the shell is the only reliable path.
- EvieShell is not a voice **golden** until the owner A/Bs it; the README's
  "PWA Home Screen WebRTC is GOLDEN" note is historical and no longer
  accurate for the current web capture stack.
- The conversation lease: while the Mac holds the live conversation, the
  phone's first tap can be refused with "Evie is talking on another device —
  tap again to take over". That is intentional single-conversation behavior,
  not a mic fault.
