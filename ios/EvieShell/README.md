# Evie Mobile Shell

Thin iOS app: WKWebView hosts Evie Web Core. Native Capability Broker is the trusted body.

Product path is **not** Apple Shortcuts. The Shortcuts Bridge remains in the backend for regression only (`?legacy_bridge=1`).

## Check without Xcode.app

```sh
cd ios/EvieShell
swift build
swift run EvieBrokerCheck
# or: make evie-shell-check
```

That compiles the typed broker + origin rules on macOS. It does **not** compile the iPhone app (needs iOS SDK / Xcode).

## Voice

PWA Home Screen WebRTC is **GOLDEN** (owner verified clean). Native WKWebView voice is **UNKNOWN** until a physical A/B on the Primary iPhone. Option C (keep PWA voice while proving capabilities) is the current gate. Do not retune frozen live-voice files.

## Install on a phone

See `DISTRIBUTION.md`. This machine currently has Command Line Tools, not Xcode.app. Device install is an owner action: install Xcode, open `ios/EvieShell/EvieShell.xcodeproj`, set your Team, Run on the 16 Pro.

Set `EV_API_URL` in Info.plist (or build setting) to the Home Station origin, typically `https://<machine>.ts.net`.
