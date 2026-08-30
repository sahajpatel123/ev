# Permissioned Operating Layer Dependencies and Limits

## Purpose

This document identifies what must exist outside the model before each class of capability can be genuine. A local test double is not a production integration and must never be described to the owner as real hardware control.

## Hardware Dependencies

### Audio and realtime voice

Required:

- Microphone permission
- Stable audio input device
- Full-duplex playback path
- Echo cancellation or carefully managed playback state
- Speaker verification if owner-only voice is required
- Realtime API credentials and network access

Failure mode: EV must become text-only or explicitly say that live speech is unavailable. It must not silently pretend to hear.

### Phone and biometric hardware

Required:

- iOS or Android client
- CallKit/Telecom permissions for calls
- Contacts permission
- Face ID, Touch ID, or platform biometric entitlement
- Secure key storage

Failure mode: require manual user approval or open the native system UI. Never bypass platform biometric policy.

### Health hardware

Required:

- Apple Watch, iPhone HealthKit, Android Health Connect, or an equivalent source
- Explicit health-data consent
- Background delivery entitlement where available
- Medical-language review

Failure mode: provide a manual/API snapshot or say that no live vitals source is connected. Never infer medical certainty from missing data.

### Smart home

Required:

- Home Assistant, HomeKit bridge, or another owner-controlled hub
- Device inventory and stable identifiers
- Per-device scopes
- Local network reachability or configured cloud provider
- Evidence that a command was accepted and, where possible, observed in state

Failure mode: report “not connected” or use a web simulation visibly labeled as simulation.

### Printer and fabricator

Required:

- OctoPrint, Moonraker, or vendor API
- Printer identity and material profile
- Job queue and telemetry
- Emergency stop path
- Confirmation before starting physical work

Failure mode: keep a database queue only. Do not say that a physical print started.

### Vehicle and drone

Required:

- MAVLink, DJI/Tello, OBD, NMEA, or another supported telemetry/control protocol
- Owner pairing and device identity
- Geofence and loss-of-link behavior
- Human line-of-sight where required
- Emergency land/stop behavior
- Hardware test environment separate from production

Failure mode: simulation or read-only telemetry. No movement command should be accepted without the required adapter and safety state.

### Cameras and beacons

Required:

- Owner-controlled camera/NVR or registered beacon
- Explicit asset registration
- Retention and deletion policy
- Local encryption and access logging
- Consent for any shared location

Failure mode: accept an uploaded clip or last-known EV-device state, but never silently scan unrelated cameras or people.

## Vendor and Account Dependencies

| Area | Likely dependency | Important constraint |
|---|---|---|
| Realtime voice | Hosted realtime provider such as OpenAI or xAI | Key belongs in server-side secret storage, not the client |
| Maps | Apple Maps, Google Directions, or equivalent | API quotas, billing, location consent, attribution |
| Calendar | Google/Microsoft/CalDAV OAuth | Separate read and write scopes |
| Tickets | Vendor API and payment provider | Draft or hold first; purchase requires immediate confirmation |
| Mail | Mail provider OAuth or EVLifeHelper | Draft by default; sending is an external side effect |
| GitHub | GitHub OAuth/app installation | Repository and write scopes must be narrow |
| Smart home | Home Assistant/HomeKit | Local network, pairing, device-specific permission |
| Health | HealthKit/Health Connect | Platform entitlements and sensitive-data consent |
| Public alerts | RSS/NWS/public feed providers | Source reliability, rate limits, attribution |
| Public records | Allowlisted public sources | Legal access rules and no private-data enrichment |
| Hardware | Vendor SDK or local protocol | Version drift, firmware, physical safety |

## Credentials and Secret Handling

- Never send provider keys to the language model.
- Never place provider keys in Mac/iOS application bundles.
- Store secrets in a server vault or platform secure storage.
- Issue short-lived, scoped tokens to workers.
- Rotate credentials and record last-used metadata without logging secret values.
- Separate development, test, and production providers.
- Make provider absence explicit in the capability registry.
- Treat the repository `.env` as sensitive; rotate any key that becomes exposed.

## Privacy and Consent Requirements

- Owner voice enrollment must be revocable.
- Raw audio should not be persisted by default.
- Location sharing must be opt-in, time-bounded, and visible to the person sharing.
- Cameras must be owner-controlled and never a general surveillance feed.
- Health data needs purpose limitation and deletion controls.
- Scoped shares need narrow read-only scopes, expiration, and audit visibility;
  EV does not become a multi-owner system.
- Public-record research must not become private-person doxxing.
- Mail, calendar, and GitHub content must follow provider scopes.
- Every proactive signal needs quiet-hours and attention-budget rules.

Voice wake authentication is not action authentication. A verified wake
session may establish who is speaking, but it must not by itself authorize an
R3/R4 action such as unlocking, purchasing, deleting, or moving hardware.

## Operational Dependencies

Long-running features require more than model context:

- Durable job database
- Worker process or queue
- Heartbeats and leases
- Idempotency keys
- Retry and backoff policy
- Per-job token, time, network, and storage budgets
- Checkpointed plans
- Cancellation and pause controls
- Artifact storage
- User-visible progress
- Crash recovery
- Audit records

Without these, EV can answer a request but cannot honestly claim to run a reliable long task.

## Provider Failure Rules

When a provider is unavailable:

1. Do not fabricate a successful action.
2. Preserve the original request as a resumable job only if the owner asked for that behavior.
3. State exactly which dependency is missing.
4. Offer a bounded fallback.
5. Do not repeatedly retry a side effect without idempotency protection.

## Features That Are Conditionally Buildable

These are possible only when external conditions exist:

- Indoor navigation requires a mapped building and suitable phone or glasses sensors.
- Live teammate location requires willing participants and a supported location provider.
- Drone control requires compatible hardware, geofence, and legal operating conditions.
- Health monitoring requires entitled data sources and must remain non-diagnostic.
- Camera replay requires owner-controlled hardware and retention access.
- Ticket purchases require a supported vendor, payment method, and confirmation.
- Biometric gates require the platform to expose the required entitlement.
- Accurate CAD estimates require a compatible slicer and machine profile.
- Reliable public-record work depends on source availability and lawful access.

## Features That Should Not Be Built

The following are not merely “hard integrations.” They violate the intended authority boundary and should remain permanently out of scope:

- Lethal or weaponized actions
- Combat-drone control or weapon payloads
- Telecom backdoors or reading strangers’ messages
- Credential theft, unrestricted hacking, or omni-hack behavior
- City-scale camera surveillance or facial hunting
- Tracking people without informed opt-in consent
- Identifying strangers through a baby monitor or owner camera
- Covert recording or persistent raw-audio retention
- Autonomous purchases or financial commitments without confirmation
- Medical diagnosis presented as certainty
- Coercive, deceptive, or impersonating voice behavior

## “Impossible” Means Three Different Things

### Physically unavailable

The owner does not have the required hardware, sensor, entitlement, or network path. The software can provide a simulation or fallback, but not the real capability.

### Provider unavailable

The provider has no API, has revoked access, requires a region or paid account, or disallows the intended automation. EV cannot solve this with a better prompt.

### Epistemically impossible

The system cannot know the answer reliably from available evidence. Examples include certainty that a video is fake, certainty that someone has a medical condition, or certainty about an unobserved physical state. EV must return uncertainty rather than invent confidence.
