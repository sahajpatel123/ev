# Phone kernel restoration — intermediate verification

The kernel now projects phone-only tools for persisted phone identities,
bypasses the deterministic Mac send shortcut, and retains the phone catalog
after discovery. Phone receipts no longer run the Mac-capable pipeline before
Muse. These are intermediate changes, not proof of full phone feature parity.

Model results exclude signed action cards. Model-supplied confirmation IDs are
rejected and are absent from the phone tool schema. Client receipt evidence is
stored separately from server-result fields. Cross-device idempotency-key
collisions return an error instead of another phone's receipt.

── REPORT ──

FILES TOUCHED: `backend/app/cognitive/kernel.py`,
`backend/app/device_gateway/api.py`,
`backend/app/device_gateway/cognitive_text.py`,
`backend/app/device_gateway/durable_actions.py`,
`backend/app/device_gateway/mobile_actions/routes.py`,
`backend/app/device_gateway/cognitive_phone.py`,
`backend/app/device_gateway/mobile_actions/tool.py`,
`backend/app/device_gateway/turn_receipts.py`,
`backend/tests/test_cognitive_phone_adapter.py`,
`backend/tests/test_cognitive_phone_text.py`, `backend/clients/pwa/mobile-actions.js`,
`backend/clients/pwa/tests/mobile_actions_test.js`, `backend/clients/pwa/release.json`,
and this report.

COMMANDS RUN (from `backend`):

```sh
.venv/bin/python -m pytest -q tests/test_cognitive_phone_text.py tests/test_cognitive_phone_adapter.py tests/test_mobile_actions.py
.venv/bin/ruff check app/device_gateway/cognitive_text.py app/device_gateway/cognitive_phone.py app/device_gateway/turn_receipts.py app/device_gateway/mobile_actions/tool.py app/cognitive/kernel.py tests/test_cognitive_phone_text.py tests/test_cognitive_phone_adapter.py
```

MEASURED NUMBERS: 63 phone/broker tests passed in 34.15 seconds; focused lint
above passed. A preceding combined 90-test run had 25 failures and 65 passes:
a concurrent kernel edit imported `SEND_BODY_MAX`, absent from `send_intent.py`.
The Mac-only import now occurs after phone routing, isolating phone turns.
That dependency was subsequently supplied by concurrent work. A fresh
`.venv/bin/python -m pytest -q tests/test_cognitive_os_v2.py tests/test_cognitive_speed.py`
run passed 41 tests in 23.32 seconds. Lint on the existing gateway
API also reports pre-existing issues outside the new `/text` branch; unrelated
automatic formatting was restored and only that branch remains changed.
The phone-role routing tests use real persisted devices and the real kernel,
but fake model responses and a mocked action adapter. They are not physical
execution or live-authority race acceptance tests. Full-repository baseline
and suite were not rerun; concurrent unrelated changes remain in the tree.

MODELS ADDED: None.
DEP REQUEST: None.
DEPENDENCY NOTES: Agent 1 — coordinate global baseline reconciliation and
production deployment separately. No deployment is requested by this report.
Agent 11 — reconcile the concurrent `SEND_BODY_MAX` send-intent dependency;
no edits were made to `app/ev/send_intent.py` in this work.
HUMAN APPROVALS: Prior cognitive/device-gateway scope extension used. No new
credentials, permissions, commit, push, or production restart.
WHAT IS STILL NOT REAL:

- Full phone feature parity, text-only action authority, and physical acceptance
  on both owner iPhones remain unproved.
- Immutable pre-inference binding now rejects changes to live identity, lease,
  generations, instance, authorization revision, and origin before dispatch.
  Seven isolated tests cover six replacement cases and one unchanged control.
  Five additional tests now use the real registry, control-session constructor,
  lease helpers and authority helper: valid session, heartbeat extension,
  initial generation mismatch, missing revision, and replacement during a
  database refresh. Dispatch remains mocked; physical reconnect races and PCM
  authority binding are not established by these tests.
- Kernel action cards now survive model failure in a private result channel and
  persist in receipts. A committed-row replay test verifies recovery without
  another kernel call. The current client presents the last card; multi-card
  presentation and full crash/reconnect recovery still need acceptance tests.
- Same-device revoked/demoted receipt replay is now rejected by current trust.
  Historical receipt evidence provenance,
  canonical origin validation, and unknown-device fail-closed routing need work.
- Strict phone-kernel confirmation now checks the pending action's original
  session, instance and origin. Four real-broker tests cover authorizing a draft
  in the unchanged context and rejecting each changed field. These do not send
  a real message or prove the native/system confirmation UI.
- Kernel-mode trusted `/text` now uses a separate immutable phone text context.
  It reuses a matching lease, acquires one if absent, and rejects a foreign
  active lease instead of silently taking it over. It needs neither a fake
  live session nor microphone permission. Six real-kernel/broker tests cover
  both phone roles, committed receipt replay, active-Talk preservation,
  takeover, draft confirmation, and direct endpoint-handler request identity.
  Full HTTP authentication, concurrent retry, cold-process recovery and
  physical device acceptance remain unproved.
- Structured phone-tool failures now survive the kernel and receipt boundary;
  typed HTTP replies return the broker error and honest failure text instead of
  a model's misleading success-like reply. The takeover test asserts this.
- Active typed use now heartbeats its matching lease without changing the lease
  ID, live session, or generation. A targeted near-expiry test passed separately
  after the full focused run; it verifies renewal beyond ten seconds.
- Typed cancellation now precedes generic cognitive reflexes when a phone draft
  is pending. Four real-broker tests verify cancel/never mind/no/stop, durable
  cancellation, a returned cancelled card, no extra model call, and preservation
  of an unrelated global goal. Voice cancellation still needs equivalent proof.
- `node --test backend/clients/pwa/tests/mobile_actions_test.js backend/clients/pwa/tests/phone_action_transport_test.js`
  passed all 34 client action/transport tests from the repository root.
- Recovered native cards now require a deliberate tap, including after a
  duplicate HUD. Typed replay only returns action cards for an unchanged,
  unexpired server text binding; replacement tab/lease/origin/revision tests
  verify suppression without inference or lease acquisition. Typed replay now
  reconciles terminal action state against durable and memory records, stripping
  execution payloads from cancelled/completed/expired cards. Three cache-loss
  tests exercise this with real touch-cancel and client-completion handlers;
  those handlers now persist final state. Physical execution remains unproved.
- `.venv/bin/python -m app.scripts.gen_release_manifest` regenerated the asset
  manifest. `.venv/bin/python -m pytest -q tests/test_release_contract.py` passed
  all 10 tests in 9.31 seconds. No production process was restarted.
- Concurrent insertion remains open and prevents full phone acceptance.

Agents: technical-audio-designer and qa-director (read-only reviews integrated).
