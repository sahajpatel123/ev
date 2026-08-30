# Permissioned Operating Layer Implementation Roadmap

**Authority:** This document defines the order for permission and operating-layer work. `docs/BUILDABLE_FEATURES_PLAN.md` remains the 49-item feature backlog and catalog. It must not maintain a competing wave schedule.

## Operating Principle

POL is the law over the existing code, not a greenfield operating system. Reuse the current tool/action specs, validation, integration registry, life policy, identity and trust matrix, protocol sheet, delegates, approved actions, access logs, RQ workers, research sessions, maker queue, callouts, timers, and realtime voice path.

Do not create a fourth capability registry, a parallel memory taxonomy, a new durable-job framework, or an emergency execution lane unless an existing surface is proven insufficient by a failing real-entry-point test.

## Phase 0: Thin Authority Trunk

**Goal:** Make policy decide real existing tools within one short implementation cycle.

Deliver:

- Add `risk_class`, confirmation policy, and evidence shape to existing tool/action specs.
- Define one `evaluate_policy()` function used by voice tool calls and HTTP/action execution.
- Reconcile `TOOL_SPECS` and `ACTION_SPECS` behavior without immediately deleting either source.
- Reject unknown names and missing scopes deterministically.
- Make missing providers return `not_connected`, never simulated success.
- Route four real capabilities through the policy path: weather, diagnostics, calendar read, and one existing life action.

Required tests:

- Unknown capability is rejected.
- R0/R1 owner reads run without unnecessary confirmation.
- Owner standing R0–R2 scope works after Training Wheels.
- R3/R4 cannot skip fresh confirmation.
- Missing provider returns an honest result.
- Evidence includes source and timestamp.

Exit criterion: policy is deciding real requests, not merely validating a new schema.

## Phase 1: Voice Honesty and Continuity

Reuse the current live voice path to make authority visible without making speech feel bureaucratic.

Deliver:

- Spoken protocol sheet from the existing protocol/Training Wheels surfaces.
- Honest provider failure speech.
- Pause, resume, cancel, and sleep behavior.
- Quiet hours applied to proactive spoken callouts.
- Approval hold behavior: “Confirm it on your phone” while realtime voice remains alive.
- Target-bound confirmation events for high-risk actions.
- Wake verification kept separate from action authorization.

Do not add new model vendors or hardware in this phase.

## Phase 2: Existing Read-Only Voice and HUD Wiring

Make already-real capabilities useful through the existing voice and HUD entry points:

- Diagnostics and calibration
- Weather
- Research with citations
- Calendar read
- Gear, battery, and storage
- Memory search
- Health snapshot summary with source and timestamp
- Briefings and anomaly explanations

Read-only and reversible capabilities use the lighter R0/R1 gate. They still require scope, audit, source/timestamp evidence, and honest language, but not unnecessary cancellation or idempotency machinery.

## Phase 3: Life I/O

Build the daily partner value before software automation or a hardware catalog:

- Timers and reminders using existing `OwnerTimer` and callout paths
- Calendar writes with write scope, target evidence, and duplicate protection
- Calls with named confirmation and call evidence
- Mail drafts by default; send under owner standing scope or confirmation
- GitHub/calendar/inbox digest using existing provider scopes
- One tiny software allowlist only after life I/O: open an approved URL, run a named test, or apply a patch in one approved workspace

Do not build a general coding-agent platform here. EV may expose narrow software capabilities when they solve an actual owner workflow and remain bounded.

Exit criterion: the owner uses these flows as the real assistant for 14 days without duplicate sends, unclear authority, or fabricated success.

## Phase 4: One Physical Actuator

Choose exactly one physical integration that the owner will use this month, such as:

- Home Assistant light control, or
- Printer queue/start through OctoPrint/Moonraker.

Implement the complete path:

- Owner pairing and target ownership
- Adapter and local double
- R3 confirmation
- Evidence of accepted and observed state
- Timeout and cancellation
- Audit entry
- Honest `not_connected` fallback

Stop and dogfood for two weeks before adding another actuator.

## Phase 5: Demand-Driven Extensions

The following are catalog items, not a scheduled phase:

- Maps and indoor navigation
- HealthKit/Health Connect
- Cameras and NVR
- Vehicle and drone telemetry
- Leashed drone control
- Beacons
- CAD/slicer estimation
- Glasses renderers
- Public records and public alert adapters
- Best-effort media forensics
- Cross-device continuation beyond the currently useful devices

Pull an item into active work only when the owner has the hardware/provider and a concrete monthly workflow. Every item still follows Phase 0 policy and the implementation gate.

## Delegation and Biometrics

Delegation remains a scoped, read-only share, not a second owner or general multi-user mode. Keep it out of the daily delivery trunk until single-owner behavior is boring and reliable.

Biometrics are a last-mile approval factor for high-risk actions, not a foundation dependency for read-only voice. Add platform gates only where the target platform exposes a reliable entitlement.

## Testing Strategy

### Policy tests

- Scope evaluation
- Standing R0–R2 owner autonomy
- Fresh R3/R4 confirmation and target binding
- Confirmation expiry
- Unknown capability rejection
- Provider absence
- Evidence source and timestamp
- Voice wake auth versus action auth

### Integration tests

- Voice tool to existing action/tool registry
- Existing registry to integration adapter
- Worker restart and resume for existing jobs
- Provider timeout and recovery
- Device revocation
- Local double versus provider mode

### Acceptance tests

- Owner asks by voice and receives a truthful result.
- An unverified speaker cannot invoke owner-only actions.
- A missing provider produces a clear fallback.
- A high-risk voice request waits for independent confirmation.
- A realtime approval hold does not break the voice session.
- A barge-in cancels speech without cancelling a durable job unless requested.
- The audit record proves what happened.
- The owner uses the shipped flows for 14 days.

## Success Criteria

The operating layer is ready for broader capability work when:

1. Existing registries share one enforceable policy decision path.
2. EV can explain enabled, setup-required, unavailable, and refused capabilities.
3. R0–R2 owner autonomy feels natural and remains visible/revocable.
4. R3/R4 actions cannot execute through voice wake authentication alone.
5. Provider and hardware absence is visible rather than simulated.
6. Existing jobs survive process restarts before new job infrastructure is proposed.
7. The owner has used the active flows for 14 days.
8. The model can propose more work without granting itself more authority.
