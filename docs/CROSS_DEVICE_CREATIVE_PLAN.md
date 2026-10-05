# Cross-Device Creativity Plan — MacBook + iPhone 16 Pro + iPhone SE 2020

**Status:** exploratory plan, 2026-10-02. Nothing here is implemented unless
noted "SHIPPED". Fleet law still binds every later change (OWNS paths,
append-only shared files, additive API contract, offline determinism, no lies).

## 1. What exists today (measured, not prose)

| Surface | Where | What it does |
| --- | --- | --- |
| Evie Everywhere API | `backend/app/api/everywhere.py` + `backend/app/everywhere/` | One typed command contract for project/goal/commitment ops, command receipts, conflict/replay outcomes |
| Routed actions | `app/everywhere/device_actions.py` | Deterministic broker: phone asks, Mac executes; allow-list of safe caps (`device.echo`, `mac.notify`, `computer.open_calculator`, …), idempotency, offline queue |
| Capability registry | `app/everywhere/capabilities.py` | Per-device capability advertisement, presence-filtered |
| Continuity | `app/everywhere/continuity.py`, `handoff_context.py` | Cross-turn offer/ledger projection across devices |
| Presence TTL | `app/device_gateway/presence.py`, `everywhere/devices.py` | ONLINE/RECENTLY_SEEN/DEGRADED/OFFLINE per device |
| Device gateway | `backend/app/device_gateway/` (40+ modules) | Mobile/mac auth, push, handoff, WebRTC, durable actions, tickets, phone scope |
| Clients | `ios/` (EVApp SwiftUI, Watch, Share ext), `macos/` (SPM, EV runtime), plus Safari PWA at `/evie/` (locked primary iPhone product) | Thin clients over the one backend |

**Honest gap:** all of this is *routed actions + mirrored state*. What is missing
is the *ambient, eventful* layer — devices that notice each other, follow the
owner, and trade small verbs continuously instead of only when told.

## 2. Creative features nobody in this space does (proposal deck)

Anything below marked NEXT is a candidate for implementation; the rest are
directional, to be pulled in when the NEXT ones prove out.

1. **Follow-Me Intent Bus** — NEXT. One typed, durable-ish queue where any
   device can drop a short-lived intent (`focus.handoff`, `clipboard.push`,
   `nudge`, `look.request`, `screen.share`, `media.duck`) addressed to a
   *capability* rather than a device. A deterministic resolver picks the
   best online device for that capability (primary-phone-first, recency,
   form factor), delivers, and records a receipt. Every device can read the
   same bus, so a phone can watch what the Mac is being asked to do.
2. **Primary Device Arbiter** — NEXT. A pure function that answers "where
   is the owner right now?" from presence TTL, recency, and form factor
   scores. Notifications, wake follow-through, and chat-context focus
   default to the primary device; handoff moves when the primary changes.
   No guessing, no ML — deterministic, auditable, offline.
3. **Pocket Executive** — a focus/automation router: on the primary device,
   "I'm heading out" triggers a bundle (SE becomes remote, Mac locks down,
   digest paused). `heading_out.py` exists as scaffolding; this completes
   the loop via the Intent Bus.
4. **Panopticon Lite / Cross-Device Look & Screen** — `look.request` on the
   bus lets the Mac (or SE) ask the 16 Pro for a photo or screen capture;
   the capture flows through the existing capture pipeline with provenance.
5. **Clipboard Constellation** — `clipboard.push` intents sync text/URLs
   (and image refs later) across all three devices with per-item TTL and
   source device chip. Explicit permission chip before cross-device read.
6. **Mac as Remote-Desktop for Phones** — reverse direction of `device_actions`:
   from the Mac, request `device.echo`, `mac.echo`-style canaries, photo
   capture, or an AppIntent run on a chosen phone; receipts flow back.
7. **Unified Presence HUD** — one card on every device listing the other
   two: online/offline, what it last did (from the bus receipts), battery
   if advertised. Builds on `devices.py` + telemetry.
8. **Wake-Word Handoff** — when Evie wakes on the Mac but the owner is
   stationary closer to a phone (recency signal), the turn is mirrored to
   the primary device's thread instead of splitting conversations.

## 3. Non-goals (so this stays Evie)

- No new product domain, no multi-user, no cloud relay — everything stays
  on Tailscale + the one backend.
- No silent capability use: every cross-device verb leaves a receipt and a
  provenance chip.
- No fake persistence: the bus is in-memory by default with a deterministic
  on-disk journal option; tests never require Redis/Postgres.

## 4. Implementation order

1. `app/everywhere/followme.py` — Intent Bus + receipts (this plan ships it).
2. `app/everywhere/primary_device.py` — Primary Device Arbiter + tests.
3. `api/everywhere.py` additions under one `# --- AGENT FOLLOWME ---` block:
   `POST /v1/everywhere/intents`, `GET /v1/everywhere/intents`,
   `POST /v1/everywhere/intents/{id}/ack`, `GET /v1/everywhere/primary`.
4. Contract regen left to Agent 1 at merge (`make update-contract`); this
   change is additive-only.
5. Native surfaces later: macOS menubar "hand off to phone", iOS 16 Pro
   share-sheet capture into the bus. Safari PWA `/evie/` remains the
   day-one phone surface.

## 6. Status (2026-10-02)

| Feature | State |
| --- | --- |
| Follow-Me Intent Bus | SHIPPED backend + `/intents` routes + tests |
| Primary Device Arbiter | SHIPPED `/primary` + tests |
| Pocket Executive | SHIPPED `/pocket-executive/heading-out` + tests |
| Cross-Device Look & Screen | SHIPPED `/look/request` + tests |
| Clipboard Constellation | SHIPPED `/clipboard(/push)` + tests |
| Mac→Phone remote control | SHIPPED `/remote/request` + tests |
| Unified Presence HUD | SHIPPED `/presence/hud` + tests |
| Wake-Word Handoff | SHIPPED `/wake/mirror` + tests |
| iOS Follow tab | SHIPPED `FollowMeTabView` in EVUI + EVApp hub view |
| macOS Follow-Me | SHIPPED `FollowMeView` in menubar + `FollowMeModel` |

Swift surfaces are unverified locally — build/run in Xcode on each device.
Known honest gaps: bus/clipboard state is in-memory; `presence_hud` battery is
null unless a device advertises it; iPhone PWA background wake remains a
foreground concern.

## 7. Evie Mesh (2026-10-03) — proximity-aware cross-device layer

Creative additions beyond the original deck: BLE proximity zones feeding the
existing Intent Bus resolver, camera/shortcut/sensor/nudge/clipboard/migrate/
mac-verb intents, per-device mesh execution, and human approval for risky Mac
verbs.

**Backend (SHIPPED, 73 tests green):**
- `app/everywhere/mesh.py` — `ProximityStore` (BLE RSSI observations, 60s TTL;
  advertisements, 300s TTL), zones immediate/near/far/edge, proximity-biased
  resolvers (`converge_targets`, `escalation_chain`, `_camera_target`,
  `_mac_target`), 8 intent vertices, `SENSOR_REGISTRY` (R1/R2), `MAC_VERBS`
  risk table, `mesh_status`.
- `followme.py` — 8 new intent kinds; `resolve_target`/`pick_primary` take an
  optional proximity map (presence rank → proximity rank → age → device id).
- `capabilities.py` — `mesh`, `converge`, `shortcut`, `sensor` bases.
- `api/everywhere.py` — 12 new routes: proximity observe/read, mesh
  advertise/status, converge, photo/capture, shortcut/run, sensor/read,
  nudge/escalate, clipboard/transform, conversation/migrate, mac/verb.
  observe/advertise require a device identity (master key gets 401
  DEVICE_REQUIRED).
- `followme_features.py` — presence HUD fills `battery_percent` from mesh
  advertisements (closes the known battery=null gap).
- Contract regenerated: `make update-contract` → 542 v1 paths (was 520).

**Swift (SHIPPED, builds verified):**
- `ios/EVClient/Sources/EVClient/EvieMesh.swift` — shared BLE advertiser/
  scanner (2s scan / 3s rest), roster, `EvieMeshStore`, API client extension
  for all 12 routes.
- `macos/Sources/EV/` — `MeshIntentPoller` (10s poll, executes+acks, R2 mac
  verbs held for Allow/Deny approval), `MeshConvergeService` (chime/speak/
  flash), `MacBatteryReader` (IOKit), `FollowMeView` mesh section,
  `AppModel` wiring.
- `ios/EVApp/` — `MeshIntentRunner` (converge haptics+sound, camera capture,
  shortcut URL, barometer/battery/steps/GPS/torch via HealthKit/CoreMotion/
  CoreLocation, nudge notification, clipboard tidy; honest FAILED acks when a
  sensor or permission is missing), `MeshSettings` view, "Converge with EV"
  AppIntent phrase, mesh toolbar entry.

**Verified locally (2026-10-03):**
- `uv run pytest tests/test_mesh_proximity.py tests/test_mesh_endpoints.py
  tests/test_followme_bus.py tests/test_followme_features.py
  tests/test_everywhere_g2.py -q` → 73 passed.
- `ruff check` on all touched backend files → clean; `mypy` on the three
  touched modules → clean.
- `swift build` in `ios/EVClient` and `macos/` → Build complete.
- iOS app: `xcodebuild build -scheme EVApp -destination
  'generic/platform=iOS Simulator' CODE_SIGNING_ALLOWED=NO` → **BUILD
  SUCCEEDED** (via a /tmp copy of the project; see watch note below).

**Latent iOS bugs fixed (first-ever iOS build surfaced them):**
- `CameraFrameCapture.swift` — `AVCaptureDevice.isExposureTargetBiasSupported`
  does not exist; guard is now `maxExposureTargetBias > 0`.
- `HealthKitManager.swift` — `HKUnit.kilogram()` does not exist; VO2 max unit
  is now `literUnit(with: .milli)` ÷ (`gramUnit(with: .kilo)` × minute),
  i.e. the correct mL/kg/min.
- `AppConfig.swift` — missing `import Foundation` (URL).
- `MeshIntentRunner.swift` — `Set.removeFirst(Int)` → `removeAll()`, `await`
  on `UIApplication.open` and `UNUserNotificationCenter.add`.

**Known honest gaps:**
- Watch targets: Xcode 27 removed WatchKit extensions
  (`com.apple.product-type.watchkit2-extension` deprecated). The whole
  xcodeproj cannot build with them present under Xcode 27 — a pre-existing
  condition. iOS verification used a /tmp project copy with watch targets
  stripped; the real project still needs a single-target watchOS migration
  before it builds in Xcode 27. No mesh code was added to the watch.
- Convergence chime/speak/flash require the app running (no background
  execution); BLE scanning cycles only while foregrounded on iOS.
- Mac R2 verbs stay pending until a human allows them in the menubar.
- Proximity is BLE-radio-local only; intent routing stays on Tailscale HTTP.
- `baseline.json`/AGENTS.md §5 not rewritten — tree contains other agents'
  uncommitted work; Agent 1 linearizes at merge.
