# Living glass continuation — 2026-09-09

The existing Atelier layout and source material are retained. The centerpiece
is now version `living-glass-2` (`#orb.dataset.presenceVersion`).

## Implemented

- Real-time animation clock, targeting 30 rendered frames per second. The old
  idle path rendered at roughly 12 fps and capped elapsed time at 50 ms per
  80 ms frame, slowing its already small movement.
- Approximately five-second breathing, softly asymmetric scaling, floating
  translation/rotation, and alpha-masked champagne/sage light inside the glass.
- Pointer parallax and a short decaying touch response; no permission request,
  microphone activation, extra navigation control, or fabricated voice level.
- Connection errors no longer freeze the decorative object. Only measured
  voice amplitude drives the active audio response.
- Reduced Motion is static. Offscreen, background and modal-covered rendering
  pauses; returning restarts without a large elapsed-time jump.
- Mobile action presentation/status hooks now resynchronize modal placement.

## Verification performed on this continuation

```sh
node --test backend/clients/pwa/tests/presence_motion_test.js backend/clients/pwa/tests/webrtc_lifecycle_test.js backend/clients/pwa/tests/mobile_actions_test.js backend/clients/pwa/tests/secondary_controls_test.js backend/clients/pwa/tests/phone_working_features_test.js
node backend/clients/pwa/audio_scheduler_test.js
node --check backend/clients/pwa/presence.js
node --check backend/clients/pwa/app.js
node --check backend/clients/pwa/webrtc.js
node --check backend/clients/pwa/mobile-actions.js
```

76 Node tests passed, including six real-renderer clock/lifecycle tests using
an instrumented canvas. Audio scheduler check and four syntax checks passed.
These tests do not measure actual iPhone frame rate or rendered image quality.

From `backend`:

```sh
.venv/bin/python -m pytest -q tests/test_release_contract.py tests/test_pwa_audio.py tests/test_iphone_capability_plan.py tests/test_webrtc_connection.py
.venv/bin/ruff check clients/pwa/tests
.venv/bin/mypy clients/pwa/tests/quiet_room_ui.py clients/pwa/tests/phone_controls_ui.py
.venv/bin/python -m app.scripts.gen_release_manifest
```

81 focused Python tests passed. Ruff and mypy passed (mypy notes that untyped
function bodies are not checked). This is not a full repository verification.

The preceding isolated WebKit functional run exercised actual pairing, hello,
reload, Talk and Stop handlers with simulated network/WebRTC boundaries. It
passed, but predates this animation revision. The earlier seven-layout visual
suite stopped on a camera-denial fixture timeout after four layouts; it is not
reported as green. A new real-time clock assertion was added to that suite.

## Pending visual and physical acceptance

The previous isolated-browser launch was rejected by the approval service with
a usage-limit error. A retry approval has been requested; no alternate route was
used to bypass that rejection. The advertised Browser skill file was also no
longer present when rechecked in this session.

On an iPhone, reload Evie and record ten seconds of the home screen. Check the
breathing/light motion, touch response, Tools pause/resume, and returning from
the background. With iOS Reduce Motion enabled, the object should remain still.
Then test Talk, Stop during connection, microphone denial/retry, and audible
reply. Report Safari versus Home Screen app, phone model, and any visible error;
do not send pairing codes or tokens.

## Report

FILES TOUCHED: `presence.js`, `app.js` (action observer integration only on this
continuation), `release.json`, `tests/presence_motion_test.js`,
`tests/quiet_room_ui.py`, and this report. Prior control/voice edits remain in
the shared tree and were rechecked, not replaced.

COMMANDS RUN / MEASURED NUMBERS: Exact commands and results above.

MODELS ADDED: None; existing material asset reused without regeneration.

DEP REQUEST: None.

DEPENDENCY NOTES: Agent 1 — server-side lease-generation fencing remains
separate from the client's serialized cleanup; full product acceptance remains
open. Agent 17/client coordination — complete the pending visual/phone checks.

HUMAN APPROVALS: Isolated browser retry requested. No commits, pushes,
production restarts, migrations, or live external actions performed.

WHAT IS STILL NOT REAL: Current rendered animation and physical iPhone voice
are not yet verified. Native/OS handoffs remain device-dependent; failed drafts
and receipt retries are page-local, not a durable offline outbox. The full goal
has not been marked complete.

Agents: technical-audio-designer, qa-director, secondary-controls specialist.
