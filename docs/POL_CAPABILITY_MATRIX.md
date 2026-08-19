# Permissioned Operating Layer Capability Matrix

This matrix maps the 49 buildable features to the operating-layer primitives they require. It is a coverage checklist, not the delivery schedule. The delivery order lives only in `POL_IMPLEMENTATION_ROADMAP.md`; the feature backlog remains in `BUILDABLE_FEATURES_PLAN.md`.

## Readiness Labels

- **Existing substrate:** a relevant code path exists, but it may still need product wiring and test-backed integration. This label does not mean the feature is complete.
- **Buildable:** software work plus ordinary account or provider setup.
- **Hardware-bound:** requires owner hardware or a platform entitlement.
- **Provider-bound:** depends on an external API, account, or vendor policy.
- **Safety-bound:** requires additional privacy, medical, financial, or authorization review.
- **Not guaranteed:** technically possible only under conditions that may not exist on the owner’s machine.

## Voice and Personality, 1–10

| # | Capability | Operating-layer work | Dependencies | Readiness |
|---:|---|---|---|---|
| 1 | Named voice companion | Unified conversation identity, wake/session continuity, cross-device routing | Voice lifecycle, device registry | Existing substrate |
| 2 | Assistant nickname | Persist display name and inject it into identity and TTS context | Preference store, policy for who may change it | Existing substrate |
| 3 | Dry, loyal personality and self-status | Enforce response style and expose verified subsystem health | Personality, diagnostics, honesty filter | Existing substrate |
| 4 | Capability introduction | Generate a capability sheet from enabled registry entries | Feature flags, protocol sheet | Existing substrate |
| 5 | Honest protocol list | Report enabled, setup-required, unavailable, and refused capabilities | Capability registry | Existing substrate |
| 6 | Welcome by owner name | Bind verified identity to greeting and session start | Speaker verification, identity store | Existing substrate |
| 7 | Dedication message | Store short owner-authored text/audio and play under a one-time policy | Consent, encrypted storage | Buildable |
| 8 | Social advice and small talk | Add companionship mode, time budget, and isolation honesty rules | Personality, attention policy | Safety-bound |
| 9 | Quiet hours and attention budget | Apply proactive policy to voice, HUD, workers, and notifications | Notification policy, scheduler | Existing substrate |
| 10 | Status callouts | Event-to-speech bus with deduplication and replayable callout history | Attention policy, HUD | Buildable |

## House, Lab, and Devices, 11–25

| # | Capability | Operating-layer work | Dependencies | Readiness |
|---:|---|---|---|---|
| 11 | One voice across devices | Shared session identity, transcript replication, attention routing | Device registry, realtime transport | Existing substrate |
| 12 | Import preferences to a new device | Pairing ceremony and scoped preference synchronization | Identity, registry, secure pairing | Buildable |
| 13 | Lights, locks, security | Typed smart-home actions with evidence and fresh confirmation | Home Assistant/HomeKit, owner devices | Hardware-bound / R3 |
| 14 | Place calls | Resolve owner contacts, confirm, call through native helper, report evidence | CallKit, contacts, phone | Provider-bound / R3 |
| 15 | Timers and reminders | Durable scheduler, restart survival, spoken callout delivery | Worker daemon, attention policy | Existing substrate |
| 16 | Routes and leave-by | Routing adapter, location consent, ETA evidence, calendar join | Apple/Google Maps, calendar | Provider-bound |
| 17 | Indoor navigation | Indoor map model, phone AR, later glasses renderer | ARKit/ARCore, owner-authored floorplan | Hardware-bound |
| 18 | Calendar and tickets | Calendar write scope, draft/hold flow, payment confirmation | Calendar OAuth, ticket vendor, payment | Provider-bound / R4 |
| 19 | Training Wheels | Persistent checklist and feature gates | Permissions, calibration, registry | Buildable |
| 20 | Teach gear modes | Per-device capability and mode schemas | Device adapters | Buildable |
| 21 | Voice control of systems | Allowlisted actuators with target ownership and confirmation | Device registry, smart home, drone later | Buildable / R2–R3 |
| 22 | Consumable warnings | Threshold monitoring, inventory reconciliation, proactive callout | Gear/BOM, scheduler | Buildable |
| 23 | Offline or seized-device lock | Revoke, stop capture, expire sessions, lock local caches | Identity and device revocation | Safety-bound |
| 24 | Delegated account access | Time-boxed role grants, scope restrictions, immediate revocation | Access log, identity, consent | Safety-bound |
| 25 | Biometric unlock | Gate high-risk approvals with platform biometrics | Face ID/Touch ID/WebAuthn | Hardware-bound |

## Workbench, Research, Health, and Environment, 26–49

| # | Capability | Operating-layer work | Dependencies | Readiness |
|---:|---|---|---|---|
| 26 | Calibration by voice | Run diagnostics as a typed job and summarize worst check | Diagnostics API, HUD | Existing substrate |
| 27 | Workbench diagnostics | Continuous probe, live strip, callouts on state changes | Worker, metrics, attention policy | Buildable |
| 28 | HUD cards everywhere | One versioned card schema rendered by Mac, web, phone, watch, later glasses | Device clients, renderer contracts | Existing substrate |
| 29 | Research with sources | Research job, citations preserved through voice, artifact storage | Search, source policy, memory | Existing substrate |
| 30 | 3D printer/fabricator | Durable print job, provider adapter, telemetry, confirmation | OctoPrint/Moonraker, printer | Hardware-bound / R3 |
| 31 | Suit and desk continuity | Live transcript and shared conversation across wearable/workstation | Registry, sync transport | Existing substrate |
| 32 | CAD/design estimates | File ingestion, slicer integration, bounded estimation | STL/STEP/SVG, slicer CLI | Hardware/software-bound |
| 33 | Vehicle/drone test monitoring | Telemetry ingest, live HUD, thresholds, test session | MAVLink/NMEA/OBD, sensors | Hardware-bound |
| 34 | Battery/storage status | Heartbeat telemetry and spoken query | Client permissions, registry | Existing substrate |
| 35 | Wearable vitals | Consent-based health ingestion and anomaly explanation | HealthKit/Health Connect | Hardware-bound / sensitive |
| 36 | Head-injury screening | Fixed safety script, symptom checklist, escalation guidance | Medical review, emergency contacts | Safety-bound |
| 37 | Weather/environment | Voice tool and HUD integration of public weather data | Open-Meteo, coarse location | Existing substrate |
| 38 | Team brief | Briefing compiler, HUD card, delegate-only sharing | Tactical data, delegation | Buildable / R2 |
| 39 | Opted-in teammate location | Consent ledger, expiring location pings, map card | Find My/location provider | Provider-bound / sensitive |
| 40 | Owner camera replay | Owner camera adapter, clip fetch, vision summary | RTSP/HomeKit/NVR | Hardware-bound / sensitive |
| 40a | Live camera look | One-shot capture, on-device OCR, enrolled object/person match, spoken evidence | Device camera, vision stack, optional hosted DeepSeek-OCR | Existing substrate / sensitive |
| 41 | Public alerts and scanner digest | Subscription adapters, deduplication, source attribution | RSS/NWS/public feeds | Provider-bound |
| 42 | Best-effort fake-video analysis | Heuristic/model pipeline with uncertainty and reasons | Vision, forensic review | Safety-bound |
| 43 | Voice changer/accessibility | Voice profile selection and audio accessibility settings | TTS provider capabilities | Buildable / provider-bound |
| 44 | Public records research | Allowlisted public-source adapters and refusal rules | Search, legal/source policy | Provider-bound / safety-bound |
| 45 | Leashed owner drone | Geofence, takeoff confirmation, telemetry, emergency land | Drone SDK, hardware, LOS | Hardware-bound / R3 |
| 46 | Owner gear beacon | Registered asset model and consented location ingestion | BLE/AirTag/Find My provider | Hardware-bound / sensitive |
| 47 | Rough structure estimates | Image/floorplan measurements with confidence labels | Vision, scale reference | Buildable / uncertain |
| 48 | Quiet anomaly sensing | Fused signals, explainable reason list, attention budget | Health, calendar, alerts, scheduler | Safety-bound |
| 49 | Company/calendar/inbox help | Cross-source digest, drafts, approval-based sending | Mail/GitHub/calendar scopes | Provider-bound / R2 |

## Policy Risk

Readiness and policy risk are separate dimensions. Use the risk classes in
`PERMISSIONED_OPERATING_LAYER.md`; do not infer authorization from the
readiness label or from model confidence. A feature can be technically ready
and still require R3/R4 confirmation.

## Delivery Authority

There is deliberately no second feature wave list here. Use
`POL_IMPLEMENTATION_ROADMAP.md` for the single delivery order and this matrix
for coverage, dependencies, and demand-driven backlog selection.
