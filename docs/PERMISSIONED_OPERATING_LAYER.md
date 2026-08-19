# EV Permissioned Operating Layer

**Status:** initial four-capability authority path implemented and test-backed. This document does not grant capabilities or authorize implementation by itself.

## Purpose

EV should become a permissioned operating layer around the model gateway. The gateway may route realtime voice or reasoning work to OpenAI, xAI, DeepSeek, or another provider. The model supplies language understanding, planning, summarization, and tool selection. EV supplies identity, authority, memory, execution, device integration, evidence, persistence, and recovery.

The initial target is a capable personal partner across voice, Mac, phone, web, and background workers. Workshop hardware and other actuators are extension points, not prerequisites for the operating layer.

## Core Rule

Every action follows this boundary:

```text
spoken request
  -> model interprets intent
  -> EV validates identity and scope
  -> policy decides whether approval is required
  -> typed adapter performs one bounded action
  -> adapter returns evidence
  -> EV reports result honestly and records an audit event
```

The model never receives raw credentials, unrestricted process access, arbitrary network access, or direct authority to invent a new actuator.

## System Layers

### 1. Interaction layer

Responsibilities:

- Realtime voice and text conversation
- Partial and final transcripts
- Barge-in and cancellation
- HUD cards and progress updates
- Device routing and conversation continuity
- Clear spoken confirmation and failure language

This layer should remain replaceable. Voice is one interface to the operating layer, not the operating layer itself.

### 2. Intent layer

Convert model output into a typed intent, never an executable command string.

Example:

```json
{
  "intent": "calendar.create_event",
  "arguments": {
    "title": "Dinner",
    "starts_at": "2026-08-21T19:00:00-04:00"
  },
  "requested_by": "owner",
  "request_id": "req_01J..."
}
```

The intent layer rejects unknown actions, missing arguments, ambiguous targets, unsupported providers, and requests outside the current authority scope.

### 3. Policy and authority layer

This is the central security boundary. It answers:

- Who is asking?
- Which device and session heard the request?
- Is the speaker verified?
- Is this action allowed for this actor?
- Is the target owned, shared, public, or unknown?
- Does the action require confirmation?
- Is the action reversible?
- Is quiet-hours or emergency policy relevant?

Policy must be deterministic and inspectable. The model may explain policy, but it must not override policy.

### 4. Existing registries and policy convergence

POL is a hardening specification over the existing `TOOL_SPECS`,
`ACTION_SPECS`, fleet tool specs, `IntegrationRegistry`, and `ApprovedAction`
surfaces. Do not create a fourth registry or a parallel YAML source of truth.

The first implementation step is one `evaluate_policy()` path used by voice
tool execution and HTTP/action execution. Registry unification is a later
cleanup only after parity tests prove that no scope or behavior is lost.

Each capability declaration should eventually include:

Required fields:

- Stable capability name and version
- Human description
- Input schema
- Output schema
- Required scopes
- Risk class
- Confirmation policy
- Target ownership requirement
- Provider and fallback status
- Idempotency behavior
- Timeout and retry policy
- Audit event type
- Cancellation behavior

### 5. Adapter layer

Adapters translate typed EV capabilities to real systems:

- Realtime and reasoning providers through the model gateway
- Mac and iOS helpers
- Home Assistant or HomeKit
- Google Calendar and Maps
- Mail, contacts, and calling
- OctoPrint, Moonraker, or slicers
- HealthKit and Health Connect
- Camera and NVR systems
- Vehicle, drone, and beacon providers

Adapters must expose `local` doubles for deterministic tests and a separate `provider` mode for owner-configured machines.

### 6. Execution layer

Use two execution primitives:

- **Request:** a short, user-visible operation with a tight timeout.
- **Job:** durable work with checkpoints, progress, cancellation, and resume.

Scheduled work is a job with a clock. A job that pauses for approval is a
supervised job. Panic-lock and medical guidance are named policy flows, not an
emergency execution lane or an authority bypass. Reuse existing RQ workers,
research sessions, maker queue, and owner timers until they demonstrably fail
to provide the required behavior.

### 7. Memory and evidence layer

EV already has fact, preference, observation, and decision concepts. POL does
not introduce a second memory taxonomy. The hard rule is that model guesses
must not be written as facts; source type, provenance, and uncertainty must
survive the write path.

Actions must return evidence such as provider IDs, timestamps, device acknowledgements, URLs, job IDs, or measured readings. “I did it” is not evidence.

### 8. Audit and recovery layer

Record:

- Request and normalized intent
- Actor, device, session, and authentication level
- Policy decision and required scope
- Confirmation event
- Adapter/provider used
- Result and evidence
- Errors, retries, cancellation, and rollback

Sensitive payloads should be minimized or encrypted. Audit records must support incident review without storing unnecessary raw audio or private content.

## Authority Model

### Actors

- **Owner:** full authority within configured limits
- **Scoped share:** explicitly scoped, time-boxed, revocable read-only access;
  never a second owner or general-purpose guest mode
- **Device:** can submit only capabilities allowed for its identity
- **Worker:** can execute only the job scopes it was issued
- **Model:** proposes intents; never independently receives authority
- **Provider:** reports observations; never becomes an EV owner

### Risk classes

| Class | Examples | Default behavior |
|---|---|---|
| R0, read-only local | status, weather, own calendar read | Run automatically |
| R1, reversible local | volume, HUD layout, dismiss card | Run automatically with audit |
| R2, external side effect | send draft-approved mail, create calendar event | Owner standing scope after Training Wheels, or confirmation |
| R3, physical or privacy-sensitive | unlock door, call person, camera clip, drone movement | Fresh confirmation plus strong auth |
| R4, irreversible or financial | purchase, delete, revoke, destructive file change | Explicit confirmation immediately before action |
| Forbidden | weapons, wiretaps, unauthorized surveillance, credential theft | Refuse; do not create an adapter |

Risk is attached to the capability and target, not guessed from conversational tone.

## Confirmation Design

A confirmation must identify the action, target, consequence, and scope:

> “Call Ned on FaceTime now?”

Not:

> “Should I continue?”

Confirmation expires after a short period, cannot be silently reused for a different target, and must be bound to the same authenticated session. Destructive actions require a stronger confirmation than “yes.”

For the owner on owner-controlled devices, standing approval is the default for
bounded R0–R2 actions after Training Wheels. R3 and R4 always require fresh,
target-bound approval with a short TTL. Voice wake verification is not the same
as action authorization: high-risk actions should use a HUD tap, platform
biometric, or another independent factor where available. Standing approvals
must be visible, revocable, and periodically reviewed.

Realtime voice must not stall while an approval is pending. EV should speak a
short hold line such as “I have the request ready; confirm it on your phone,”
keep the voice session alive, and deliver the result only after the policy
decision completes.

## Long-Running Work

Long-running AI work must not remain inside a voice request task. The correct pattern is:

1. Create a durable job with an owner, goal, allowed tools, budget, and deadline.
2. Speak a concise acknowledgement and job ID.
3. Execute in a worker with checkpoints.
4. Pause before side effects that exceed the job’s authority.
5. Emit progress only under attention policy.
6. Save artifacts, citations, and evidence.
7. Ask for approval when required.
8. Support cancel, pause, resume, and retry.
9. Deliver a concise result plus links to the full record.

The model may re-plan within the job scope, but it may not expand its own scopes.

## Computer and Software Control

EV should not expose a raw unrestricted terminal as its first actuator. Use layered capabilities:

- Read a named file or project
- Search a bounded workspace
- Apply a structured patch
- Run a named test command
- Start a named development service
- Open an approved application or URL
- Inspect process status
- Request a privileged operation for approval

For advanced coding tasks, use an isolated workspace, resource limits, network policy, secret redaction, patch review, and test gates. The owner should see what changed before deployment or destructive operations.

## Model Provider Boundary

The configured model provider can provide:

- Realtime speech-to-speech
- Function/tool calling
- Structured outputs
- Vision and document understanding
- Planning and summarization
- Research synthesis with supplied sources
- Multi-turn context

No model provider can by itself provide:

- Permission to access this Mac
- A valid HomeKit, Maps, HealthKit, mail, or printer credential
- Reliable proof that an external action succeeded
- Durable background execution after a request ends
- Device ownership or biometric authority
- A safe policy boundary around arbitrary tools

EV must provide those parts.

## Implementation Gate

For R0/R1 read-only or reversible capabilities, the minimum gate is:

1. A typed contract.
2. An authorization scope.
3. Audit evidence with source and timestamp.
4. A real-entry-point test.
5. Honest UI and voice language.

For R2+ capabilities, add the full gate: local double, provider or explicit
“not connected” result, target-bound confirmation, cancellation, idempotency,
rollback where possible, and complete audit evidence.

## Agent Handoffs

These are contracts, not new registries. Agent 1 owns the authority decision;
downstream agents consume these fields and return evidence to the same audit
path.

### Agent 2 — observation and privacy boundary

- Camera capabilities must declare `name`, `version`, `camera` target scope,
  owner-asset binding, risk `R3`, confirmation `fresh`, and provider state.
- Person/object recognition is limited to an owner-controlled asset and an
  explicitly scoped observation request. Recognition must return uncertainty,
  source, timestamp, model/provider, and evidence IDs; it must not authorize a
  person or create a private-person identity claim by itself.
- Capture, derived embeddings, sightings, and clips require separate
  retention/deletion authorization. Deletion is target-bound, audited, and
  revocable only by the owner; raw media is not retained by default.

### Agent 3 — interaction and approval boundary

- Consume the live manifest returned by `GET /v1/capabilities`; do not copy
  capability metadata into client configuration.
- Render `allow`, `deny`, `confirm`, `not_connected`, `invalid_request`, and
  `unavailable` as visible policy results. A confirmation hold contains the
  capability, normalized target, required scopes, expiry, device/session, and
  approval action ID.
- Approval events are `approval.requested`, `approval.confirmed`,
  `approval.expired`, `approval.denied`, and `approval.resumed`. Wake or speaker
  authentication is never substituted for the independent R3/R4 factor.

### Agent 4 — execution and provider boundary

- Every tool/job request carries the capability name/version, actor, device,
  session, required scopes, request ID, and idempotency key where declared.
- Provider state is explicit: `available`, `not_connected`, or `unavailable`;
  adapter errors must not be reported as successful execution.
- Results use the shared evidence shape: `{source, timestamp}` plus
  provider-specific IDs, accepted/observed state, and error/timeout/cancel
  fields when applicable. Retries and cancellation are cooperative and are
  recorded in the existing access log; no second job framework is introduced.
