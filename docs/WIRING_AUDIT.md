# Wiring Audit — does live Evie use app.fabric? (WP0, 2026-10-18)

Branch `evie-fabric` @ `e3acaff`. Verdict first: **no existing code imports
`app.fabric`; the live Evie does not use the fabric.** Proven three ways
(grep, import graph at runtime, live HTTP trace). WP1 wires it in.

## 1. RED FLAG verdict (with evidence)

| Proof | Command | Result |
|---|---|---|
| Repo-wide grep for `app\.fabric` | `grep -rn` over `backend/` | 54 matches, **all inside `backend/tests/e2e_fabric/`**; zero in `backend/app` outside `app/fabric/` itself (which uses only relative `from .x` imports) |
| Router mounts | `grep include_router backend/app/main.py` | 26 routers mounted; **no fabric router** (main.py:296-328) |
| Runtime trace | Boot ASGI app, `POST /v1/chat` → 200, `POST /v1/cognitive/turn` → 200, then inspect `sys.modules` | `app.fabric imported: False`; only `app.digital.fabric` (unrelated digital-ops module) present |

So the 41 e2e_fabric scenario tests prove the fabric works against its own
lanes, not that Evie uses it. Every entrypoint below reaches the brain
through `app/cognitive/kernel.py::handle_turn` (line 380) or a legacy
pipeline — never through `app/fabric/run_turn.py::run_turn` (which today is
a **sync explicit-ops dispatcher**, not an LLM loop, and is called only by
e2e tests).

## 2. Entrypoint wiring table (before → after)

"Before" = this tree. "After" = WP1 target: fabric branch at the top of each
conversational entrypoint; `EV_FABRIC=1` → `arun_turn()` facade → kernel with
fabric tools; `EV_FABRIC=0` → today's code byte-identical (golden-tested).

| # | Entrypoint | Handler (file:line) | Before: next call | Kernel? | Fabric? | After (WP1) |
|---|---|---|---|---|---|---|
| 1 | CLI `ev ask` | `backend/clients/cli/__init__.py:344` `ask()` / `:389` `ask_stream()` | `POST /v1/chat` → `run_chat_pipeline` (core.py:1308) → `handle_turn` iff `kernel_mode_active()` (core.py:1718-1773) | conditional | No | DONE (S23): CLI posts /v1/chat → pipeline → facade; thin-client test pins it |
| 2 | Hotkey ⇧⌘E (macOS) | `macos/Sources/EV/AppModel.swift:245` → `toggleAudioControl()` → `LiveConversation.start()` | `POST /v1/voice/live/open` + `WS /v1/voice/live` → `serve_live_websocket` (voice.py:690) → `LiveSession` → `handle_turn_maybe_remote` (session.py:2293) | yes | No | DONE (S23): `handle_turn_maybe_remote` funnels the facade |
| 3 | Talk sidecar :18000 | `scripts/start_talk_sidecar.py:474` (same `app.main:app`, `EV_COGNITIVE_ROLE=voice_edge`) | Voice-edge turns HTTP-forward to kernel `:8000` (kernel.py:1585 `handle_turn_maybe_remote`) | yes (remote) | No | DONE (S23): same funnel as #2 |
| 4 | PWA text | `backend/clients/pwa/app.js:4562` → `POST /v1/device-gateway/text` | `user_text()` (device_gateway/api.py:863): kernel on → `run_phone_text` (cognitive_text.py:68) → `handle_turn`; else `run_trusted_device_text` → `run_chat_pipeline` | conditional | No | DONE (S23): receipts funnel the facade (prod path); legacy fallback via pipeline |
| 5 | PWA voice | `app.js:6133` `POST /v1/device-gateway/live/open` → `app.js:6394` `WS /v1/voice/live` | `live_open()` (api.py:1000) then `voice_live()` (voice.py:578) → `serve_live_websocket` → `handle_turn_maybe_remote` | yes | No | DONE (S23): same funnel as #2 |
| 6 | device-gateway text | `app/device_gateway/api.py:863` `user_text()` (+`:1893` stream) | Same as #4 | conditional | No | DONE (S23): both via receipts/pipeline funnels |
| 7 | `POST /v1/chat` | `app/api/core.py:1063` `chat()` | `run_chat_pipeline()` (:1308): kernel branch iff `kernel_mode_active()`; else local/direct-provider | conditional | No | DONE (S23/S24): pipeline kernel branch funnels the facade; legacy intact below |
| 8 | `POST /v1/cognitive/turn` | `app/api/cognitive.py:34` `cognitive_turn()` | Direct `handle_turn(...)` (:41) — thinnest kernel surface | direct | No | DONE (S23/S24): direct facade funnel + passthrough golden |
| 9a | `WS /v1/voice/hands-free` | `app/api/voice_live.py:331` → `HandsFreeResponder.respond()` (:212) | `VoiceRuntime.handle_utterance` → `run_chat_tts_pipeline` → `run_chat_pipeline` → kernel branch | conditional | No | DONE (S23): same pipeline funnel (source=voice test) |
| 9b | `WS /v1/voice/live` | `app/api/voice.py:578` `voice_live()` | `serve_live_websocket` (:690) → `LiveSession` → `handle_turn_maybe_remote` | yes | No | DONE (S23): same funnel as #2 |
| 10 | Shortcuts / App Intents | `app/api/everywhere.py:1129` `POST /v1/everywhere/shortcut/run` | `mesh.request_shortcut_run()` — routes intent to a device; **no turn, no LLM, by design** (device actuation, not conversation) | no | No | No facade (not a turn). WP11 adds turn-backed shortcut actions that call the facade |
| 11 | routines/scheduler | `routines/service.py:158` `_route_action_for_run()` → `runtime_service.route_action/execute_action` | `ev.tools.dispatch` directly (runtime.py:1240-1262). **No turn, by design** (scheduled provenance, approval-aware) | no | No | No facade (not a turn). Fabric policy becomes the approval authority in front of `dispatch` effects; new dispatch tools give routines fabric lanes |
| 12 | workers/RQ | `app/workers/jobs.py` (memory/retention/research jobs) | No turn function (background jobs, by design). Exception: cognitive delegation worker calls `handle_turn` directly (delegation.py:432-439) | no / worker yes | No | Delegation worker covered by kernel (no change); RQ jobs unchanged |

Non-conversational surfaces (#10–#12) intentionally do NOT route through a
turn facade: they carry `scheduled`/device provenance and must never look
like owner utterances (R8). Their WP1 integration is the policy engine +
new dispatch tools, not `run_turn`.

## 3. Brain internals (what the facade plugs into)

- `handle_turn()` (kernel.py:380, async): reflex fast paths → MiMo
  `chat_with_tools` loop (≤16 steps, kernel.py:1263) with specs from
  `tool_specs_for_turn()` (speed.py:89) over `SEMANTIC_TOOLS` (31 tools,
  capabilities.py:9) → per-call `execute_semantic()` (executor.py:119).
- `execute_semantic` routes to existing backends: `ev.tools.dispatch`
  (via `_run_existing`/`_backed_existing`), `digital.tools`, memory, etc.
  Phone turns get `phone_turn_specs()` (cognitive_phone.py:196) =
  semantic bus + phone-local pair, so new `SEMANTIC_TOOLS` automatically
  reach phone turns.
- `capability.discover` → `public_descriptors()` (capabilities.py:599) =
  `SEMANTIC_TOOLS` + digital-fabric descriptors. New tools appear here
  automatically once added to `SEMANTIC_TOOLS`.
- Provider for kernel turns: `require_text_provider()` (kernel.py:1061).
  Tests script it via `monkeypatch.setattr("app.gateway.roles.require_text_provider", …)`
  (established pattern, e.g. tests/test_owner_every_turn.py:202).
- Legacy direct-tool surface: `TOOL_SPECS` (ev/tools.py:137, 107 tools) +
  `dispatch()` (ev/tools.py:2986, giant if-chain). Already contains
  `execute_command` (spec ~line 2634, handler `_run_execute_command`,
  line 3490) — the existing shell surface to rewire, not duplicate.
- `computer`/`code` dispatch tools back kernel `files.act`/`code.act`/
  `computer.*` via `_run_existing(session, "computer"|"code", …)`.

## 4. WP1 integration design (record of decision, as built)

Refinement from the first sketch: entrypoints do NOT get a branch at their
top. The funnel points are the four kernel call sites, so filter / events /
memory / lease / idempotency / receipt machinery stays intact:

1. **Facade**: `app/fabric/facade.py` — `arun_kernel_turn()` (typed core,
   returns `KernelResult`) + `arun_turn()` (dict receipt; ops mode runs the
   sync `run_turn` lane dispatcher in a thread, text mode delegates to the
   core). `EV_FABRIC=0` → transparent passthrough to `handle_turn`.
2. **Funnel points** (all lazy-import the facade; S23 proves each):
   `run_chat_pipeline` kernel branch (core.py, covers CLI/`/v1/chat`/
   hands-free/stream/legacy-device), `cognitive_turn` (cognitive.py),
   `_apply_kernel_phone_turn` (turn_receipts.py, covers PWA/device text),
   `handle_turn_maybe_remote` (kernel.py, covers hotkey/Talk/PWA-voice/
   live WS). Kernel selection is `kernel_mode_active() or fabric_enabled()`
   (D7) in phone receipts; prod (`mimo_kernel`) is a no-op, tests flip to
   kernel (legacy pins added where tests subject the legacy loop).
   **Deviation, measured at WP13:** `/v1/chat`'s kernel branch is
   `kernel_mode_active()` only. With `EV_FABRIC=1` alone it rerouted chat
   turns and silently dropped four endpoint contracts: attachment/perception
   provenance (never-send images answered without the "blocked" receipt), the
   per-endpoint provider seam (`get_chat_provider`), the output-filter report,
   and the historic 503 for a dead provider/cost cap. Production
   (`EV_COGNITIVE_MODE=mimo_kernel`) still runs chat through the kernel, and
   the safe read-only local intents (presence, capabilities, callouts,
   elapsed) answer before it so "can you hear me?" stays instant.
3. **Registration**: 8 dispatch tools (`shell_run`, `shell_session`,
   `terminal_layout`, `screen_ref`, `sql_query`, `pim_act`,
   `builder_job`, `atlas_discover`) implemented once in
   `app/fabric/dispatch_tools.py` on top of fabric lanes (policy + spans +
   receipts); 8 `SEMANTIC_TOOLS` (`shell.run`, …) routed by the executor
   via `_backed_existing`; `MUTATING`, `HOME_STATION_TOOL_NAMES`, and the
   context ladder extended. `execute_command` deliberately NOT rewired
   (it is a locked-down single-operation jail, not a shell).
4. **Policy**: fabric `decide()` fronts every new effect; CONFIRM parks an
   `ApprovedAction` via `_park_independent_hold` (resumes through the
   standard approve→`execute_action`→re-dispatch path; the dispatch-level
   `approved` flag distinguishes a bound resume from the auto-minted R2
   switch). DENY fails closed; panic denies effects outright.
5. **Spans**: `app/fabric/trace.py` (contextvar spans + ring buffer keyed
   by entrypoint). S23 asserts turn + lane spans per entrypoint through
   the real ASGI app; S24 goldens pin `EV_FABRIC=0` byte-identical to the
   `pre-fabric-baseline` tag.
6. **Kill switch**: `EV_FABRIC` setting (default on) + panic state file +
   `/v1/fabric/*` routes (15 paths, additive; contract 550→567 including
   3 pre-existing relay paths locked by the same regeneration).
7. Known gap for WP11: R2 fabric tools on device actors hold ticketless at
   `authorize` (existing policy for all R2 tools); phone turns use R1 lanes
   until ticketed holds land.

## 5. WP0 environment probe (R11)

- macOS 27.2 (26B5101f), arm64; Python 3.14.6 (backend/.venv)
- tmux 3.6b; Ghostty 1.3.1; Chrome installed; Docker 29.2.1
- Tailscale: CLI not on PATH; app binary at
  `/Applications/Tailscale.app/Contents/MacOS/Tailscale`; daemon
  **stopped** (`Tailscale status` → "Tailscale is stopped") — `ev setup`
  must start + verify Serve (Funnel OFF)
- Screenshots: no custom `com.apple.screencapture location` → ~/Desktop
  (default); ~/Desktop has ~29 `Screenshot*` files
- Signing: `EV Code Signing` (142CA1FA…, ad-hoc-style, TeamIdentifier
  unset); /Applications/Evie.app (`com.ev.suit`) signed by it 2026-10-11;
  backed up to `~/.ev/backups/Evie.app.pre-final-20261018-034404`
- launchd loaded: ev.api, ev.worker, ev.scheduler, ev.runtime,
  ev.collector, ev.talk. Defined-not-loaded: ev.ears. No plist for
  ev.hands_free / ev.whatsapp_cdp.
- Contract: 550 paths, sha256 `6f3675813479cca5…`
- Baseline full-suite run: launched 2026-10-18 in detached worktree
  `/private/tmp/ev-base-full` @ `bcf1db6` (background shell
  sh_14bee54ad001Ub4Dg12yINUVk2); JUnit →
  `…/T/opencode/baseline-junit.xml`, log → `baseline-full.log`
