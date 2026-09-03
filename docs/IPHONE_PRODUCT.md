# iPhone product path (locked)

**Primary iPhone product:** Safari (or Add to Home Screen) opening `https://<home>.ts.net/evie/` over Tailscale. Connection and app upgrades use that origin only. No Xcode, no IPA, no App Store.

`ios/EvieShell` remains optional later native wrapping. It is not required to ship or update the phone. `ios/EV.xcodeproj` (`EVApp` / Watch / Share) stays a later native track.

## One identity

One EVIE, one Home Station / Core database, one conversation identity, one active audio/output lease. Both owner-trusted iPhones share the same capability policy. Camera preference is owner-declared hardware evidence (16 Pro = preferred camera, SE = fallback), never inferred from a display name.

## Release artifact

Shippable phone artifact:

- PWA: `backend/clients/pwa/` served at `/evie/`
- Build pin: `backend/clients/pwa/release.json` (regenerate with `make pwa-release-manifest`)
- Install: Safari on Tailscale → Share → Add to Home Screen
- Upgrade: Home Station serves a new PWA build; the phone reloads via service worker + hello `latest_web_build`
- Automated gate: `make iphone-parity-check`
- Physical checklist: `scripts/ios/physical-acceptance.sh`
- Docs: `docs/EVIE_MOBILE_RELEASE.md`

API origin on the phone is always the private Tailscale HTTPS Home Station (`https://<host>.ts.net`). Never `http://<tailscale-ip>:8000`. Funnel stays off.

## Authority split

- WebRTC is the phone’s primary low-latency media transport.
- Device Gateway is the control plane: identity, leases, actions, memory/life, camera requests, receipts, durable turns.
- Frozen Mac live-voice surfaces stay frozen (`docs/FROZEN_CONTRACTS.md` and the live-voice workspace rule).

## Trust states

A phone reports exactly one of:

- `PAIRED_SANDBOX` — paired, not owner-trusted; next action is Mac promotion
- `TRUSTED_OWNER_DEVICE` — Mac-approved owner device
- `REVOKED` — credential and live sessions are dead

Clients cannot promote themselves. Promotion and revocation bump `auth_revision` and invalidate access tokens and live sessions.

## Remaining work (honest)

**Software for the Tailscale PWA path is in the tree.** Automated + broker-check gates pass. PWA pin: `2026.09.02.06`.

Still owner/device:

- Physical two-iPhone proof in Safari/Home Screen on the 16 Pro and SE.
- Optional later: EvieShell IPA, HealthKit entitlement, APNs. Not required for this path.

## Evidence classes

When claiming iPhone behavior, name the path and the evidence:

- Path: Safari PWA (primary), EvieShell IPA (optional), or native EVApp (later)
- Evidence: unit, packaged (`EvieBrokerCheck` / optional archive), or physical two-iPhone
