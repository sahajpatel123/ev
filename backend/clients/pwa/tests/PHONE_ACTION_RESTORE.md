# iPhone action restoration — transport fix and architecture dependencies

## Objective retained

Restore existing actions on both owner iPhones using Realtime for hearing and
speech and Muse Spark Contributor for reasoning. Phone-origin effects must not
silently become Mac effects. Primary product remains the private Tailscale PWA.
Neither native-only permissions nor successful physical execution are implied
by a model name, a capability list, or an automated test.

## Confirmed and fixed on the client

The PCM `handleLiveMessage()` handled replies but discarded phone-action HUDs.
It also omitted forwarding final transcripts to the action-confirmation handler.
Only WebRTC startup supplied the action module with its live session ID.

- Both transports now use `handlePhoneHud()` for action cards and receipts.
- PCM forwards final recognized text to the existing confirmation handler.
- Both transports bind the action session ID; Stop clears it.
- Stale WebRTC HUD callbacks are generation-fenced, matching PCM protection.
- WebRTC no longer presents its direct tool-result card twice.
- Progress no longer unconditionally says it is working on the MacBook.

No new capability, authorization bypass, cross-phone remote execution, or
successful external side effect is claimed by this change.

## Source-backed backend gaps — not fixed by this patch

1. `cognitive/kernel.py` supplies `device_id` to textual context, but does not
   supply authenticated phone execution context to `execute_semantic()`.
   `cognitive/capabilities.py` has no connection to the mobile-action catalog.
   Several semantic actions explicitly target Mac adapters.
2. Phone voice selection still couples Muse intelligence to PCM transport.
   Concurrent changes have added a Realtime speech-only mode; those changes
   need integration review rather than blindly replacing the current tree.
3. Some pipeline-created action records use absent live session IDs and
   placeholder instance/origin values. `_push_live()` relies on a process-local
   session lookup and silently returns without one.
4. `/complete` and `/client-complete` differ in persistence and notification.
   The cognitive HTTP result currently returns speech/metadata, not an action
   completion channel back to the originating phone.
5. Live audio queue pressure can discard HUDs without distinguishing progress
   from required phone confirmations/results. This shared live runtime is
   listed as a frozen Mac surface and was not edited here.
6. Both phones are separate supported local targets. Existing policy rejects
   remotely operating the other phone; Tailscale alone does not grant that.

## Dependency notes and required next verification

DEPENDENCY NOTE — Agent 1 / CONDUCTOR: coordinate ownership of the new
`backend/app/cognitive/**` package (not assigned in the authoritative roster)
and the changing gateway/cognitive integration. Agent 10 / CORTEX: provide a
phone-scoped capability projection/executor using server-validated device,
instance, origin, live session, trust, and capability evidence. Do not use
Mac fallback to satisfy an iPhone-local request. Agent 4 / VOICE and Agent 1:
coordinate reliable action-HUD delivery without independently editing frozen
Mac live surfaces. Agent 18 / SUIT: native-only capabilities need separate
device evidence; do not make a native install a surprise PWA prerequisite.

Required integration test: simulated Muse phone tool call → real permissioned
dispatcher → serialized live HUD → production client handler → confirmation →
durable completion receipt. Exercise each phone identity independently, stale
sessions, missing permissions, reconnect/queue pressure, and assert zero Mac
executor calls for phone-local effects. Physical proof remains separate.

## Report

FILES TOUCHED: `backend/clients/pwa/app.js`,
`backend/clients/pwa/webrtc.js`, `backend/clients/pwa/release.json`,
`backend/clients/pwa/tests/phone_action_transport_test.js`, and this report.

COMMANDS RUN:

```sh
node --test backend/clients/pwa/tests/phone_action_transport_test.js
node --test backend/clients/pwa/tests/*_test.js
node --check backend/clients/pwa/app.js
node --check backend/clients/pwa/webrtc.js
git diff --check -- backend/clients/pwa
# From backend:
.venv/bin/python -m app.scripts.gen_release_manifest
.venv/bin/python -m pytest -q tests/test_mobile_actions.py tests/test_webrtc_connection.py tests/test_release_contract.py
```

MEASURED NUMBERS: Initial transport test: 3 failures, 1 pass. After fix: 4/4
pass. Full current Node set: 83/83 pass. Focused Python set: 40/40 pass.
Syntax/whitespace checks pass; Node SHA-256 comparison verified all 13 manifest
asset hashes. Build at regeneration: `2026.09.09.04` (already selected elsewhere
in the shared tree; this patch did not change build constants).

MODELS ADDED: None. DEP REQUEST: None.
HUMAN APPROVALS: No production restarts, migrations, commits, pushes, physical
recordings, or real phone/third-party actions performed.
WHAT IS STILL NOT REAL: New-brain-to-phone execution is not integrated or
proved end to end. Both phones' current trust, served version, actual voice,
native handoffs, and receipt recovery remain unverified. The full restoration
goal remains active, not completed by the passing transport tests.

Agents: technical-audio-designer (architecture trace); qa-director inventory
attempt stopped by the agent usage limit; main performed client implementation.
