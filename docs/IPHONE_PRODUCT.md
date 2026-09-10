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

**Software for the Tailscale PWA path is in the tree.** Automated + broker-check gates pass. PWA pin: `2026.09.05.03`.

Still owner/device:

- Physical two-iPhone proof in Safari/Home Screen on the 16 Pro and SE.
- Optional later: EvieShell IPA, HealthKit entitlement, APNs. Not required for this path.

## Evidence classes

When claiming iPhone behavior, name the path and the evidence:

- Path: Safari PWA (primary), EvieShell IPA (optional), or native EVApp (later)
- Evidence: unit, packaged (`EvieBrokerCheck` / optional archive), or physical two-iPhone
<!-- Cycle EAC-10 -->
## iPhone-only Evie app-surface refresh

iPhone-only notes; the Tailscale PWA path above is unchanged. The later native
track adds a Today widget headline pair, a queue badge equal to pending offline
captures, and opt-in copy for Health, notifications, and life access.
<!-- EAC61-110 — iPhone Evie construction (50 cycles, 2026-09-08) -->
## App surface expansion (EAC61–EAC110)

The phone PWA grew from a voice/chat presence into a full Evie surface.
All additions are additive; the Mac paths and frozen live-voice surfaces
were untouched. New gateway endpoints are device-token gated and
origin-checked; every new PWA change is behind the release manifest
(`make pwa-release-manifest`).

Phone surfaces (More grid): Today (HUD/health/calendar/reminders/memory
+ quiet-hours state), Search (memories/events/reminders/contacts),
Memory browser (semantic search, detail, version provenance),
Capture (notes with privacy levels + voice notes), Look history,
Health (vitals series), Weather (structured, never guessed), People
(call/message via trusted text), Routines (digest schedule + quiet
hours), Queue (pending offline captures), Inbox (ack per item +
mark-all-read), Conversation export (copy/share), composer action
chips, capability matrix in Settings, onboarding state sync, battery
status row, pairing error polish.

Server side (all additive): `/today`, `/memories`(+detail,
+provenance), `/search`, `/capture` (notes, privacy levels),
`/capture/audio`, `/routines` (GET/PUT), `/contacts`, `/looks`,
`/vitals`, `/weather`, `/capabilities`, `/onboarding`, `/battery`,
`/inbox/ack-all`, queue item drop (DELETE), digest scheduler with
push attempts (APNs when credentials exist, honest poll otherwise),
pair rate limiting. PWA build pin now `2026.09.08.02`-era.

Native: `EvieActionPlanner` + `EvieOutcomeContract` in the broker
package (EvieBrokerCheck green); `EvieTodayPayload`,
`EvieMemoryPayload`, `EvieNotificationPayload` (EvieInbox*),
`EvieRoutinesConfig`, `EvieSearchPayload` in EVClient (EVClientCheck
green).

Verification this session: `make iphone-parity-check` green (unit +
broker), live-server browser walks of Today/Memory/Search/Capture/
Routines/Weather/chips on a paired+promoted device, Swift harnesses
green. Physical two-iPhone checks remain owner/device steps.

## Final 50-cycle gate — 2026-09-08

Automated evidence:

- `make iphone-parity-check`: **PASS** — 165 tests passed, 1 skipped;
  Swift `EvieBrokerCheck` passed.
- PWA release manifest regenerated at build `2026.09.08.02`.
- Phone-only scope guard and unsafe-tool invariants passed.
- Frozen Mac live-voice files were not edited by this construction run.

Physical evidence is intentionally **not claimed here**. Run
`scripts/ios/physical-acceptance.sh --require-physical` on the Home Station,
then complete the following on both the iPhone 16 Pro and iPhone SE:

1. Tailscale connected; Funnel disabled; open the same `/evie/` origin.
2. Pair, promote, confirm `TRUSTED_OWNER_DEVICE`, and verify the build line.
3. Run ten Talk turns, including a second turn after speech and a hearing test.
4. Test timer, reminder, Calculator, Mail, Calendar, Messages, and weather.
5. Verify a real Home Station side effect or an explicit not-connected result.
6. Verify Look preference: 16 Pro preferred, SE fallback, offline look queued.
7. Test lease transfer, revoke one phone, reconnect, and offline replay.
8. Confirm HealthKit numbers never enter captions, model speech, or receipts.

Remaining honest gaps: Safari PWA cannot directly execute iPhone Clock,
Reminders, HealthKit, cellular calls, or the iPhone address book. Those use
Home Station, native-system handoff, or an explicit unavailable response.
