# iPhone Capability Plan

Source: all 4 research angles + critic + gap-followup, parent-completed
2026-10-08 after the synthesis child dropped the camera/mac-control evidence.
Parent directly verified: `delegation.py:238-244`, `capability_manifest.py:67,100,139`,
`phone_mac.py:48-64,428-429`, `spark_phone.py:16-21`, `mobile_v2.py:90-92`,
`/camera/result` at `api.py:3871`, PWA `captureCamera`/getUserMedia, and the
`CapabilityBroker.handle()` switch (no capture case). Researcher path errors
corrected inline. Anything still unverifiable is in Open questions.

## 1. Per-capability diagnosis

### messages-auth — DIAGNOSED (evidence-backed)

**Symptom.** A phone turn answers "The phone session is no longer authorized."
There is no "still unauthorized" string; the spoken line is paraphrased from
`backend/app/cognitive/delegation.py:244` (`submit_delegate` returns it when
`capture_phone_binding` is `None` for a phone turn carrying
`device_id` + `live_session_id`).

**Root causes.**

1. **Binding mismatch, not a missing login.** `backend/app/device_gateway/cognitive_phone.py:31-54`
   plus capture at `:57-72` rejects sandbox/revoked bindings and requires the
   live session + lease to match on device/session/instance/`lease_id`/
   generation/`auth_revision`/expiry. Any drift (new lease, new generation,
   stale revision) surfaces as `PHONE_NOT_TRUSTED` / `PHONE_SESSION_REQUIRED` /
   `PHONE_LEASE_CHANGED` / `PHONE_SESSION_CHANGED` (`:265-278`), with sibling
   copy in `backend/app/device_gateway/cognitive_text.py:192`
   ("authorization changed. Please reconnect."),
   `backend/app/device_gateway/live_authority.py:31`
   ("Live session authorization changed. Reconnect."), and
   `backend/app/device_gateway/voice.py:78` ("Device authorization ended.").
2. **Pair lands in sandbox by design.** `pair_device`
   (`backend/app/device_gateway/auth.py:113-126`, `trust_level=device`,
   `memory_scope=sandbox`) and `POST /pair`
   (`backend/app/device_gateway/api.py:526`) force sandbox. Owner scope needs
   the separate grant: master-key `POST /admin/promote-owner`
   (`api.py:3989-4040`) flips scope to owner, bumps `auth_revision`, closes
   live sessions, and returns `reconnect_required:true`. A phone that paired
   but was never promoted can never hold a phone binding.
3. **Stale tokens outlive promotion.** Access tokens are `evie1` HMAC+exp
   (`auth.py:39-44`) with a revision check (`auth.py:151-154`); promotion bumps
   the revision, so pre-promotion tokens and live sessions are invalid until
   the client reconnects (`POST /session`, `api.py:548`, or Start Talk binding
   `live_session_id`). Consent gates only location/voice enrolment
   (`api.py:2167,2261`), not reads; read scopes are `messaging:read`
   (`backend/app/integrations/adapters.py:1109`) and `phone:read`/`phone:act`
   (`cognitive_phone.py:96,104`).
4. **Reads depend on Mac-side prerequisites.** iMessage recents come from the
   Mac EVLifeHelper `messages.list` subprocess
   (`backend/app/integrations/life_helper.py:7-8`,
   `backend/app/integrations/adapters.py:1148+`) needing `EV_LIFE_HELPER_PATH`
   plus TCC grants (exit 3 = `permission_denied`, `life_helper.py:17,103`).
   WhatsApp reads prefer read-only `desktop_cache` (ChatStorage.sqlite,
   `backend/app/ev/messaging/whatsapp_local.py:1-6`) else headless CDP WhatsApp
   Web (`backend/app/ev/messaging/whatsapp_cdp.py:734-741`), implemented by
   `BackgroundWhatsAppBacking` (`backend/app/digital/adapters/whatsapp.py:486-552`)
   with `FakeWhatsAppBacking` (`:86`) as the hermetic double; logged-out shows
   `session_logged_out`/qr (`:202-208`). Sends stay blocked to `life.send`
   (`adapters.py:1143-1147`).

### camera — DIAGNOSED (evidence-backed, parent-verified)

**Symptom.** Asked to describe what the iPhone camera sees, Evie says to
"complete the camera setup". Repo-wide search for that literal (literal,
regex, case-insensitive) returns **zero hits** — the line is a model
paraphrase of the gates below, not a code constant.

**Root causes.**

1. **Sandbox trust gate.** `backend/app/device_gateway/capability_manifest.py:67,100,133-141`:
   `PAIRED_SANDBOX` => "Camera look is off: no photo understanding from this
   phone.", `camera_look=false`, upgrade_hint "promote this iPhone".
   `backend/app/device_gateway/status.py:21-27`: sandbox
   `next_action='promote_on_mac'`. An unpromoted phone can never do camera
   look — same unlock ladder as messages (Phase 2).
2. **OS permission + ranking gate.** `backend/app/everywhere/endpoint_profile.py`
   tracks camera granted/denied/undetermined; denied endpoints are excluded
   from the camera pool and `resolve_camera_target` ranks by
   `camera_preference_rank`; `backend/app/everywhere/capabilities.py:94-110`
   yields `camera.look` AVAILABLE or PERMISSION_REQUIRED.
3. **PWA path is wired end to end; native path is not.**
   PWA: `backend/app/device_gateway/pipeline.py` (`needs_camera`,
   `resolve_camera_target`) => `backend/clients/pwa/app.js`
   (`captureCamera`, getUserMedia) => `POST /v1/device-gateway/camera/result`
   (`backend/app/device_gateway/api.py:3871`) =>
   `backend/app/device_gateway/phone_look.py` ingest =>
   `GET /looks` history. Native: `ios/EvieShell/App/CapabilityBroker.swift`
   `handle()` has **no camera-capture case** (only `requestPermission`), yet
   `advertisedPayload()` lists `"camera"` in `endpoint_capabilities` —
   advertise-but-no-handler. (Researcher path notes corrected by parent:
   `phone_look.py` lives under `device_gateway/`, not `ev/`; the
   `needs_camera` pipeline is `device_gateway/pipeline.py`, not
   `voice/pipeline.py`.)
4. **Mac-camera protocol is a separate lane.** `backend/app/ev/protocols.py:335-344`
   desk-live `sight/Camera` = `needs_setup` unless the Mac live client is
   connected and image-ready; `backend/app/ev/look.py:77-84` canonical spoken
   strings are "No camera source is currently connected" /
   "macOS hasn't granted EV camera access" — the Mac half of the paraphrase.

**Unlock ladder (camera):** 1) promote device on Mac (sandbox->trusted);
2) `POST /hello` with `camera` capability + `permissions.camera=granted`
(PWA: allow getUserMedia; native: `requestPermission` camera grant);
3) set camera role (`evie_camera_role` + `PUT /onboarding`, `api.py:3637-3660`)
so rank puts this phone first; 4) keep phone foreground/ONLINE.

### mac-control — DIAGNOSED (evidence-backed, parent-verified)

**Symptom.** Asked to observe/control the MacBook screen, Evie says to
"complete the Mac control setup". Regex search for that literal across
backend+macos+ios returns **zero hits** — composed model paraphrase of the
`needs_setup` / `not_connected` operator lines, not a constant.

**Root causes.**

1. **Phone lane deliberately blocks observe/control tools.**
   `backend/app/device_gateway/phone_mac.py:48-64` `_BLOCKED` includes
   `ui_action`, `inspect_ui`, `screen_look`, `app_action`, `computer`
   (plus `execute_command`, `open_url`, …); `:428-429` resolves any of them
   to `None`. `backend/app/ev/spark_phone.py:16-21` `PHONE_MAC_TOOLS` exposes
   only `open_app` / `close_app` / list / `computer_status` — no observe, no
   control. So even a fully set-up Mac cannot be observed/controlled **from a
   phone turn**: the capability the model advertises is stripped in the phone
   lane. This is the architectural gap behind "claims broad capabilities,
   refuses on the phone". (Parent-verified by direct read; researcher-cited
   `:553-574` dispatch range not re-verified line-by-line.)
2. **Mac agent is real but needs a live session + TCC grants.**
   `macos/Sources/EV/MacControlService.swift:115-159` handles status,
   list/open/activate/close apps, open_url, inspect_ui, ui_action,
   screen_look, app_action, keyboard, window/file ops; Talk sidecar on
   127.0.0.1:18000 (`scripts/start_talk_sidecar.py`); live-WS
   `ComputerRequestEvent` (`backend/app/voice/live/session.py:1845-1865`)
   with 14s/10s `_live_command` timeout (`backend/app/ev/computer.py:739-745`).
   Gating: `backend/app/ev/protocols.py:292-304` `computer_control=needs_setup`
   unless `generic_ui_control_ready`; `backend/app/ev/computer.py:701-705,781`
   "EV.app is not connected for Mac UI control"; `backend/app/ev/capabilities.py:210`
   "connect EV.app for Mac control".
3. **Permissions are per-Mac Settings panes.**
   `MacControlService.swift:66-84` snapshots `AXIsProcessTrusted` +
   Screen-Recording preflight; `:843-852` screen_look denial speaks the
   Screen Recording fix; `:919-931` accessibility denial points at the
   Accessibility pane; `PermissionCenter.swift` registers the Settings URLs.
   Stale grants need remove/re-add of EV.

**Unlock ladder (mac-control):** 1) launch EV.app with a live Talk session;
2) grant Accessibility + Screen Recording in System Settings (remove/re-add EV
if stale); 3) verify `computer_status` speaks "I can operate apps" with
`generic_ui_control_ready=true`; 4) **code change required** (Phase 4b): phone
lane must stop stripping `screen_look`/`inspect_ui` (read-only observe) for
trusted-owner devices — control (`ui_action`) stays Mac-local until a
separate explicit grant exists.

## 2. Phased implementation plan

### Phase 1 — Make re-auth recoverable in the phone UX (highest leverage)

Why: root causes 1-3 mean every lease/generation/revision drift ends the turn
with "no longer authorized" and no path forward.

1. Map failure codes to a reconnect action: on `PHONE_SESSION_REQUIRED`,
   `PHONE_LEASE_CHANGED`, `PHONE_SESSION_CHANGED`, `PHONE_NOT_TRUSTED`, and the
   delegation spoken line, the client calls `POST /session` (rebind
   `live_session_id`) and retries once.
   Files: `backend/app/device_gateway/cognitive_phone.py`,
   `backend/app/cognitive/delegation.py`.
   Tests: extend `backend/tests/test_iphone_capability_plan.py` with
   stale-lease-then-reconnect; gate: existing lease/auth tests still pass.
   DONE 2026-10-08 (live-poll half): `webrtc.js` `_pollEvents` 409 ->
   `onState("lease_lost")` + stop (was: spin on dead lease every 280ms);
   `app.js` `lease_lost` -> bounded `scheduleVoiceRecovery` (same guard as
   `failed`). Test: `backend/clients/pwa/tests/webrtc_lease_lost_test.js`
   (2/2; 22 lifecycle neighbors green). Turn-path retry still open.
   DONE 2026-10-09 (turn-path half): session-code tool failures
   (`PHONE_LEASE_CHANGED` / `PHONE_SESSION_CHANGED` / `PHONE_CONTEXT_CHANGED`)
   now also enqueue a machine-readable `reconnect_required` live event
   (`ReconnectRequiredEvent` in `app/voice/live/events.py`, best-effort
   `notify_reconnect_required()` in `cognitive_phone.py`); the delegation
   `:244` gate attaches `error_code: PHONE_SESSION_CHANGED` and notifies
   too. PWA `_pollEvents` surfaces the event as `reconnect_required` ->
   same bounded re-talk recovery as `lease_lost`. Tests:
   `test_stale_lease_failure_notifies_phone_to_reconnect` (real registry +
   lease; 27/27 file green) + node poll-event test (25/25 with lifecycle
   neighbors). Full auto-retry of the failed utterance itself still open.
2. Surface `reconnect_required:true` from `POST /admin/promote-owner` as a
   blocking client banner ("Reconnect to finish owner setup").
   Files: `backend/app/device_gateway/api.py`.
   Tests: promotion test asserts banner payload; gate: sandbox-forcing pair
   tests still pass.

### Phase 2 — Document and test the unlock ladder

Why: root cause 2 makes "paired but never promoted" the common stuck state.

1. iPhone ladder: master `POST /pairing-tokens` (`api.py:457`) ->
   `POST /pair` (`:501`) -> `POST /admin/promote-owner` (`:3989`) ->
   reconnect (`POST /session`, `:548` / Start Talk binds `live_session_id`).
   Mac ladder: set `EV_LIFE_HELPER_PATH` + TCC grants.
   WhatsApp ladder: link background session or sync Desktop app.
   Files: this doc + `backend/app/device_gateway/api.py` (no logic change).
   Tests: end-to-end pair->promote->reconnect->phone-turn test in
   `backend/tests/test_iphone_capability_plan.py`; gate: full file green.
2. Assert token-revision invalidation explicitly: pre-promotion token rejected
   after promotion until `POST /session`.
   Files: `backend/app/device_gateway/auth.py`.
   Tests: revision test; gate: no change to HMAC/exp format.

### Phase 3 — Harden message reads behind their prerequisites

Why: root cause 4; reads fail for Mac-side reasons that look like auth bugs.

1. iMessage: DONE 2026-10-09 — already structured, now pinned: exit 3 raises
   `LifePermissionDeniedError` naming System Settings → Privacy & Security
   (`life_helper.py`); `test_life_bridges.py` asserts the TCC fix is in the
   message (35 passed, 1 skipped). No product change needed.
2. WhatsApp: DONE 2026-10-09 — `_whatsapp_read_error_message()` in
   `adapters.py` maps the backing result to the true cause: logged-out/QR
   (link-device steps), `desktop_cache_unavailable` (open Desktop to sync),
   dead CDP transport, `chat_not_found`/`ambiguous_recipient` (named chat,
   never "connection"). Exception TYPE unchanged
   (`LifeHelperUnavailableError`), so no caller contract moves; only the
   message becomes actionable. `desktop_cache`-first ordering untouched;
   sends still blocked to `life.send`. Tests: 3 new
   (`test_whatsapp_background_adapter.py`); both WhatsApp files 48/48 green.

### Phase 4a — Camera: fix the advertise-but-no-handler gap (native)

Why: PWA camera look is wired end to end; EvieShell advertises `"camera"`
but `handle()` has no capture case, so native turns always fall through to
UNSUPPORTED and the model paraphrases a "setup" refusal.

1. Add a `capture` case to `CapabilityBroker.handle()` that captures a still
   via the existing `CameraFrameCapture` path and POSTs it to
   `/v1/device-gateway/camera/result` with the `camera_request_id`, mirroring
   the PWA `captureCamera` flow; on denial return a typed
   `PERMISSION_REQUIRED` (never UNSUPPORTED).
   Files: `ios/EvieShell/App/CapabilityBroker.swift`.
   Tests: broker harness asserts capture case present + denial shape; gate:
   `EvieBrokerCheck` green.
2. WITHDRAWN 2026-10-08 (parent-verified, do not implement): EvieShell
   grants media-capture to the trusted webview
   (`NativeBridge.requestMediaCapturePermissionFor` -> `.grant`), so the
   PWA `captureCamera`/getUserMedia flow works inside the shell and the
   `"camera"` advertisement is TRUE there — removing it would make the
   shell-hosted PWA believe camera is unavailable when it works. The broker
   `handle()` needs no capture case because capture happens in-webview. A
   native capture case is only needed if a pure-native (non-webview) turn
   path ever needs it.

### Phase 4b — Mac observe from phone: unblock read-only tools for trusted owners

Why: root cause 1 makes phone-side observe impossible by code, not by setup.
Unblocking observe (`screen_look`, `inspect_ui` — read-only) closes the
"claims capabilities, refuses" gap; control (`ui_action`, `app_action`,
`computer`) stays blocked until a separate explicit owner grant exists.

1. In `backend/app/device_gateway/phone_mac.py`, exempt `screen_look` and
   `inspect_ui` from `_BLOCKED` when the calling device is a trusted-owner
   device (same `memory_scope != sandbox` trust used by the camera gate);
   sandbox phones keep the full block. Add `screen_look` to the phone
   `PHONE_MAC_TOOLS` surface in `backend/app/ev/spark_phone.py` with a
   read-only description.
   Files: `backend/app/device_gateway/phone_mac.py`,
   `backend/app/ev/spark_phone.py`.
   Tests: trusted phone `screen_look` reaches the Mac agent mock; sandbox
   phone still gets None; `ui_action` still blocked for both; gate:
   `test_phone_mac*` + `test_spark_phone*` green.
2. Document the Mac-side ladder (EV.app live Talk + Accessibility/Screen
   Recording grants + stale-grant remove/re-add) in `docs/IPHONE_PRODUCT.md`
   so "Mac control setup" is a checklist, not a mystery error.
   Gate: no code change; doc only.

### Phase 4c — Stop the model advertising what the phone lane strips

Why: the capabilities answer (`GET /v1/device-gateway/capabilities` ->
`capability_manifest`) feeds the model's "what can you do" speech, but the
phone lane strips tools afterwards. Until 4b ships, the manifest must not
promise Mac observe/control or native camera to a client that cannot run them.

1. DONE 2026-10-08 (computer-control half): `capability_manifest.py` forces
   `tools.computer_action=False` for every phone (the phone lane blocks
   observe/control even when trusted) and adds a trusted-limits entry
   stating Mac observe/control is Mac-side with what phones can do instead.
   Camera scoping dropped: `camera_look: trusted` is already correct for the
   PWA lead track AND the shell-hosted PWA (webview camera grant, see 4a-2
   withdrawal); no client-kind signal exists on `Device` to scope further.
   Files: `backend/app/device_gateway/capability_manifest.py`.
   Tests: `test_trusted_manifest_does_not_promise_blocked_mac_control` (+2
   stale pins corrected with enforcement citations); full
   `test_phone_capabilities.py` 40/40 green, ruff clean. SSE voice shape
   untouched (no voice files edited).

## 3. What must not regress

- Pair always lands sandbox (`memory_scope=sandbox`, `PAIRED_SANDBOX`) despite
  any client owner claim; only master-key promote-owner flips scope and bumps
  `auth_revision`. Proved by: pair/promotion tests in
  `backend/tests/test_iphone_capability_plan.py` after every phase.
- Stale lease/session/token is rejected (401/409 family), never silently
  accepted. Proved by: stale-lease and token-revision tests, same file.
- WhatsApp sends stay blocked to `life.send`; logged-out never fabricates
  threads. Proved by: `test_whatsapp_background_adapter.py`,
  `test_whatsapp_reads_regression.py` after Phase 3.
- Offline suite stays green with no keys/weights (doubles set
  `degraded=true`). Proved by: `make test` per repo law after every phase.

## 4. Risks and rollout notes

- Reconnect-and-retry (Phase 1) must retry at most once to avoid auth-storm
  loops against a revoked device.
- Promotion closes live sessions; ship the `reconnect_required` banner in the
  same release or owners will read it as a new outage.
- WhatsApp CDP fallback is the flakiest leg (QR/logged-out states); keep
  `desktop_cache` first and the hermetic `FakeWhatsAppBacking` for tests.
- Roll out Phase 1 -> 2 -> 3 in order; each phase's gates above are the
  ship/no-ship signal.

## 5. Open questions (unverifiable from supplied evidence)

- Critic correction (parent-verified at `delegation.py:238-244`): the `:244`
  gate only fires when `live_session_id` is **present but invalid**; a
  missing/empty `live_session_id` skips the block (binding stays None, no
  failure). Phase 1 reconnect-retry therefore applies to present-but-stale
  sessions only. Also unverified: whether the kernel lane or the Mac-side
  `messages.list` subprocess path enforces phone binding at all — the "recents
  need live binding" claim is overbroad on current evidence.
- Promote self-invalidates: promote-owner bumps `auth_revision` + closes live
  sessions, so the OLD `live_session_id` fails until reconnect; no evidence of
  client auto-discard. Phase 1 banner must ship in the same release.
- WhatsApp: binding failure vs backing `session_logged_out`/qr (`whatsapp.py:202-208`)
  not distinguished for the failing turn; Phase 3 must surface which one.
- Which iPhone client the reporter used (PWA vs EvieShell vs EVApp) — decides
  whether the camera failure was the native gap (Phase 4a) or the trust gate.
- Exact `api.py` line anchors for `/pairing-tokens` (`:457`), `/pair` (`:501`),
  `/session` (`:548`), consent (`:2167,2261`) were taken from evidence and not
  re-verified line-by-line; paths verified, numbers trusted from evidence.
- Same for `cognitive_phone.py` capture/binding ranges (`:31-72`, scopes
  `:96,104`), `auth.py` ranges (`:39-44,113-126,151-154`), `adapters.py`
  (`:1109,1143-1148+`), `life_helper.py` (`:7-8,17,103`), `whatsapp.py`
  (`:86,202-208,486-552`), `whatsapp_local.py` (`:1-6`), `whatsapp_cdp.py`
  (`:734-741`), `cognitive_text.py:192`, `live_authority.py:31`, `voice.py:78`.
