# EV — Frozen Owner-Verified Contracts

> **Engineering law:** Owner pass creates a permanent regression contract. Every future deployment must respect all contracts below. See `PLAN.md` frozen-contract section.

**Last updated:** 2026-08-25 (commit eb838fe)

---

## How to read this file

Each contract has:

- **Status** — `OWNER VERIFIED / FROZEN` (physical owner proof) or `INTERNALLY VERIFIED`
- **Acceptance** — date + evidence (startup-trace, device logs, manual owner confirmation)
- **Behavioral contract** — what the owner experiences, stated without implementation detail
- **Invariants** — authority boundaries that must not be broken even though implementation may evolve
- **Regression tests** — executable test family that protects the contract
- **Physical-only** — aspects automation cannot fully prove (owner gesture, real mic, real provider)
- **Dependencies / Debt** — known follow-ups that are not regressions

---

## G1 — EVIE CORE STATE

- **Status:** `OWNER VERIFIED / FROZEN` (owner physically verified before P0; G1 code frozen since `EVIE_OS_G1_OWNER_VERIFIED` tag)
- **Acceptance:** 2026-08-24 — Project `Personal Fitness` retrieval, Goal `Improve cardiovascular fitness` create/read, cross-session persistence across 3 fresh EV.app sessions, Commitment `G1 Final Commitment Proof` create/persist/cancel, historical read, Mission Control + What Changed owner pass.
- **Behavioral contract:**
  - `Project create → read → list` returns same canonical `Project` (id, title, status, created_at) from Core/Postgres.
  - `Goal create → read` returns same `Goal` (id, title, status) and survives fresh `ConversationThread` / `ConversationState` replacement.
  - `Commitment create → read → cancel` transitions `OPEN → CANCELLED`, remains readable as historical.
  - `Mission Control` and `What Changed` return current counts + last change narrative.
- **Invariants:**
  - `Postgres / Memory OS` is canonical; `Memory` derived tables are not owner truth (Section 4).
  - `TurnGate` is authoritative owner-turn entry; deterministic router handles common intents before Luna.
  - `Capability Registry` is canonical self-capability truth; Luna never decides it.
  - Realtime never directly owns canonical Life-state tools.
- **Regression tests:** `backend/tests/test_apps_life.py`, `test_life_core.py`, `test_memory_os.py`, `test_g13_turn_controller.py`, `test_g18_live_voice_proof.py` — and the new `test_regression_golden.py::test_golden_g1` which exercises the exact owner sequence against the real `life` service.
- **Physical-only:** Fresh-session persistence across actual app quit/reopen (automation uses new DB session + new `ConversationThread` as proxy).
- **Dependencies:** `events` table, `processor` mode, `projects/goals/commitments` canonical services.
- **Debt:** None blocking.

---

## VOICE RELIABILITY — MAC + IPHONE

- **Status:** `OWNER VERIFIED / FROZEN` (Mac startup + iPhone Talk path physical pass on 2026-08-25 final build; previous 30s stall and 12-tap ritual fixed)
- **Acceptance:**
  - Mac: cold launch → mic auto-active → provider ready → conversational in single-digit seconds, no mute/unmute ritual, smooth playback (owner pass 2026-08-25).
  - Primary iPhone: open → one Talk tap → `M21 VOICE_READY` within 5s, no competing sessions, stable multi-turn (owner pass 2026-08-25).
- **Behavioral contract:**
  - Mac open → speak. No `AudioInputLease` ghost, no false `LISTENING` while provider unavailable, no 30–120s pathological stall, playback holds ~180 ms jitter buffer (no stutter, <250 ms first-word cost).
  - iPhone one Talk press → one `POST /v1/device-gateway/live/open`, one `RTCPeerConnection`, one `LiveSession`, one mic stream. Second tap while `CONNECTING` is idempotent. Session fencing never kills the just-created authoritative session. Established READY session auto-recovers once via `restartIce` or fresh generation; stale callbacks cannot affect current generation.
- **Invariants (must not regress):**
  - `AudioInputLease(.live)` means real active/starting mic, never ghost.
  - `response.done != physical playback finished`; `client playback owns truth`.
  - `cancelled response IDs / stale PCM cannot resume`.
  - `providerReadyForForward == false` → PCM withheld but `status` shows `CONNECTING`, not `LISTENING`.
  - `LiveVoiceMicrophone` single owner, no `playerNode` mutation from mic callback, no blocking work in tap, `stillThis()/isCurrent()` gates every delayed WebRTC callback.
  - `events.stream_seq` monotonic unique, `StateEpoch` stable except on lineage-replacing restore.
- **Regression tests:**
  - `test_webrtc_connection` (9 tests, SDP parts as form fields, offer validation)
  - `test_p0_transport_containment` (26 tests, transport containment)
  - `test_voice_live*`, `test_realtime_timer_chain`
  - New `test_regression_golden.py::test_golden_voice_startup_invariants` (lease ghost, ping cadence 5s×3, single-flight, fencing `except_live`, generation guards)
  - PWA `node --check` syntax for `app.js`/`webrtc.js`
- **Physical-only:** Real mic permission grant, real provider `session.created` / `remote audio track` arrival, real `RTCPeerConnection.iceConnectionState` transitions, real audio `play()` unlock, and the audible absence of stutter (automation checks queue depth / underrun counters, not ear).
- **Dependencies:** `LiveConversation.runLoop` single coordinator, `LiveVoiceMicrophone` + `TTSPlayer` (48 kHz), `EVClient.LiveVoice` ping watchdog, `backend/app/voice/live/transport.py` tick trust check, `backend/clients/pwa/*`.
- **Debt:** No further audio UX polish in this freeze; `minStartSeconds 0.18 / maxPrimeWait 0.22` are measured choices, not microbenchmark gates.

---

## G2.1 — ONE EVIE DEVICE FABRIC

- **Status:** `OWNER VERIFIED / FROZEN` (Mac create → iPhone read → iPhone field read → iPhone mutate → Mac readback physically passed on 2026-08-25)
- **Acceptance:** canonical project `G2 Continuity Canary With Normal Priority` (`fd880b0f-42dd-423e-9e2e-017252c66541`) ACTIVE/NORMAL v0 — owner created on Mac, found on iPhone, priority read NORMAL on iPhone, set HIGH on iPhone, read HIGH on Mac.
- **Behavioral contract:** One Evie, one owner (`master`), one canonical Core/Postgres, multiple trusted endpoints. Device caches are derived/disposable (delete → bootstrap rebuilds). Same `Project` id/version visible from both devices; `expected_version` conflict is explicit, never silent LWW; duplicate `command_id` → one mutation; stale read → `CONFLICT` with `current_state`; trusted phone state queries route `Device text → OwnerTurn → TurnGate → Core` (no sandbox downgrade).
- **Invariants:**
  - `StateEpoch` stable except on lineage-replacing restore; `stream_seq` owns delivery order (`CURSOR_VERSION v2|epoch|seq`), never `occurred_at`.
  - `device_trust`/`memory_scope`/`auth_revision` server-owned; client-supplied scope never grants authority.
  - `sanitized` historical recovery (original ids/timestamps preserved, `stream_seq` at ingestion) with recovery summary event, no side-effect replay.
  - Legacy `epoch|timestamp|uuid` cursors → `CURSOR_FORMAT_UPGRADE` reset.
- **Regression tests:** `tests/test_g21_vertical_slice.py` (same-owner, distinct ids, A→B read, B→A update, dedupe, CONFLICT, cache rebuild, reconnect delta, invalid cursor, revocation), `tests/test_g2_stream_ordering.py` (late-arrival, same-ts, concurrent, duplicate seq), `tests/test_g2_trust_lifecycle.py`, `tests/test_everywhere_g2.py`, `tests/test_device_gateway*`, `tests/test_g2_commands.py` field-read matrix + conversational handoff.
- **Physical-only:** Real iPhone credential possession (hash match on first authenticated call after restore), real `live/open` + `session_context` binding, real bootstrap/sync cursor exchange.
- **Dependencies:** `devices` table + `DevicePairingToken`, `everywhere/sync` (bootstrap/changes), `app/auth.data_scope`, `app/ops/state_epoch`, exclusive destructive lock (`app/ops/destructive_maintenance.exclusive_destructive_lock`).
- **Debt:** `EV_BACKUP_PASSPHRASE` still in repo `.env` (master already isolated outside repo; backup passphrase migration is a 5-minute G2.2 follow-up).

---

## Test-surface map

| Contract | Primary test file(s) | How it maps to owner proof |
|---|---|---|
| G1 Core state | `test_regression_golden.py::test_golden_g1` + `test_apps_life` | Same `life.create/list/update` calls the owner voice path uses |
| Voice startup/invariants | `test_regression_golden.py::test_golden_voice_startup_invariants` + `test_webrtc_connection` + `test_p0_transport_containment` + `swift build --package-path macos` + `node --check` | Lease ghost, ping 5s×3, single-flight, fencing, generation guards, syntax |
| G2.1 continuity | `test_regression_golden.py::test_golden_g2_cross_device` + `test_g21_vertical_slice` + `test_g2_stream_ordering` | Same `run_trusted_device_turn` + `life.*` + `changes(cursor=v2)` the PWA/Mac use |
| Device trust | `test_g2_trust_lifecycle` | Sandbox→TRUSTED→revoked lifecycle, `is_sandbox_device` branching |

---

## Change-impact declaration (required in every future Orchestrator report)

```
CHANGED SURFACES: ...
FROZEN CONTRACTS POTENTIALLY AFFECTED: ...
REGRESSION TESTS RUN: ...
RESULT: ...
```

Every generation must answer those four lines before deployment. If `CHANGED SURFACES` overlaps a frozen contract, the corresponding regression file above must be in `REGRESSION TESTS RUN`.

---

## No test weakening

Do not make a frozen test green by deleting/loosening/skipping/mocking the failed boundary unless Project Head explicitly changes the product contract. Frozen test failure = regression first.

---

## Runtime consistency

`scripts/deploy_production.sh` is the single writer; it verifies `HEAD == origin/main`, clean tree, and waits for `health.git.sha` to report the pinned SHA. A test result only applies to that SHA. See `PLAN.md` frozen-contract section.
