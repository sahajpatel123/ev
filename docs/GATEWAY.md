# EV Gateway — CORTEX (Agent 10)

Owner: Agent 10 (CORTEX). Scope: `backend/app/gateway/**`,
`backend/app/services/{tool_loop,model_call}.py`, `backend/app/tools/**`,
`backend/app/search/**`, `backend/app/ev/{tools,tool_select,actions}.py`,
`docs/GATEWAY.md`, and the streaming/local/sandbox tests.

## 1. Real token streaming (end to end)

The provider protocol is additive: the base `ChatProvider` in
`app/contracts.py` is unchanged. `app.gateway.streaming.StreamingChatProvider`
adds one method:

```python
def stream_chat(self, messages, *, model=None, temperature=0.7) -> AsyncIterator[ChatStreamChunk]
```

Implemented by `echo`, `mock`, and `mimo`. MiMo uses the
OpenAI-compatible `stream: true` chat-completions endpoint and parses SSE
lines into `ChatStreamChunk` deltas; tool-call deltas are accumulated per
index and returned on the terminal chunk.

Gemini Live (`gemini-3.8-live-extended-thinking`) is **not** this path. It
is wired in `app.voice.live.gemini_live` as a speech-to-speech realtime
socket for live talk. Typed chat stays on `EV_CHAT_PROVIDER=mimo`
(`xiaomi/mimo-v2.6-flash`).

### SSE endpoint

`POST /v1/gateway/stream` — body is the same `GatewayChatRequest` shape as
`POST /v1/gateway/chat`:

```text
event: delta
data: {"text":"…","final":false}

event: done
data: {"request_id":"…","provider":"mimo","model":"…",
       "usage":{...},"latency_ms":123.4,"first_token_ms":88.1,
       "status":"ok","envelope_hash":"…","provider_selection":{...}}
```

Blocked payloads emit `event: error` followed by `event: done` with
`status:"blocked"` and never reach the provider. A mid-stream upstream failure
emits `event: error` (typed message) followed by `event: done` with
`status:"error"` — never a truncated success. Verify byte-wise:

```bash
curl -N -X POST http://localhost:8000/v1/gateway/stream \
  -H "Authorization: Bearer $EV_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"hello"}],"stream":true}'
```

### Cancellation and connection hygiene

The provider generator closes the upstream `httpx` stream in `finally`; the
gateway generator propagates cancellation, and FastAPI's `StreamingResponse`
closes the SSE generator when the client disconnects. A dropped client cannot
leak an upstream connection. Cancellation is asserted with a slow mock (the
upstream generator's `finally` must run) in `tests/test_gateway_streaming.py`.

### Filter seam (Agent 16)

`ModelGateway.stream_chat(..., chunk_interceptor=...)` receives every raw text
delta before it is emitted. The interceptor may transform a chunk or return
`None` to suppress it.

### Audit

Every streamed call is logged through `log_model_call` exactly like a buffered
call: request envelope (with `provider_selection` and measured `cost_usd`
metadata), `envelope_hash`, usage, latency, first-token latency, status, error,
and degradation info. The hash covers the payload that crossed the boundary
and stays unchanged by selection/cost metadata.

## 2. Routing: single brain, always

MiMo is the single non-speech brain, so routing is a no-op that always
selects the configured provider. `app/gateway/routing.py` and
`scripts/routing_gate.py` state that plainly:

| Situation | Gate result | Selection reason |
| --- | --- | --- |
| Provider configured | `routing_is_noop` fails closed (`passed=False`) | `single_brain_configured` |

The gate never reports a meaningless pass: with fewer than two configured
candidates it fails closed with an explicit no-op check. Every call records
the winning provider, reason, and evidence in
`envelope.metadata["provider_selection"]`.

Unknown provider names (`EV_CHAT_PROVIDER` not in the registry) raise
`UnknownProviderError` and log at error level — no silent echo fallback.

## 3. Life agency (WAVE LIFE)

Standing owner authority: when the owner tells EVIE to do something and a
granted life bridge exists, she does it — no theatrical refusal.

### Life tools (validated schemas in `app/ev/tools.py`)

| Tool | Adapter bridge (CONDUIT) | Permission |
| --- | --- | --- |
| `send_message` | `messaging.send` | `message:send` |
| `list_messages` | `messaging.list_messages` | `message:read` |
| `resolve_contact` | `contacts.resolve` | `contacts:read` |
| `place_call` | `phone.call` | `phone:act` |
| `list_mail` | `mail.list` | `mail:read` |
| `open_url` | none yet | `life:open_url` |
| `set_reminder` | none yet | `life:reminder` |

Tools without a bridge never fake success: they return `degraded=true` plus an
exact `next_step` naming the integration/scope or helper command to grant.

### Autonomy mode

`EV_OWNER_AUTONOMY=full|confirm_unknown|confirm_all` (default `full`):

- `full` — no per-action approval for life actions inside granted standing
  scopes; the CONDUIT life policy (`app/integrations/life_policy.py`) still
  enforces scopes and the contact allowlist.
- `confirm_unknown` — known/pre-authorized contacts act; unknown recipients
  require explicit confirmation.
- `confirm_all` — every life action requires explicit approval (tool marked
  sensitive, approval gate required).

`app/ev/actions.py` exposes `autonomy_mode()`, `life_action_requires_approval()`,
and `life_agency_prompt()`.

### System prompt

When life tools are offered, the gateway appends the life-agency block to the
system message (see `life_agency_prompt()`): EVIE is the owner's agent, she
executes life actions through granted bridges, missing bridges are explained
with exactly what to grant and which helper is required, refusals are never
theatrical, and successes confirm with recipient/target, channel, and time.
This rides the MiMo path.

### Golden path

`tests/test_life_agency.py` proves: voice transcript ("text Mom I'm late") →
intent (`send_message` + `resolve_contact`) → validated tool calls → mocked
CONDUIT adapter results → spoken confirmation with evidence. The tool loop
feeds adapter results back and the final reply is spoken.

## 4. API-only reliability (timeouts, retry, circuit breaker, cost cap)

With no local fallback, an outage must degrade cleanly instead of hanging
every request. `app/gateway/reliability.py` and `app/gateway/costs.py` own the
transport policy:

- **Timeouts**: connect/read/write/pool timeouts are explicit and
  env-configurable (`EV_MODEL_CONNECT_TIMEOUT_SECONDS=10`,
  `EV_MODEL_READ_TIMEOUT_SECONDS=60`, …).
- **Retries**: transient failures (connect/timeout/5xx/429) retry with bounded
  jittered exponential backoff (`EV_MODEL_MAX_RETRIES=2`, base 0.5 s).
  Streaming never retries after the first byte; a mid-stream failure surfaces
  as a typed error.
- **Circuit breaker**: after `EV_CIRCUIT_FAILURE_THRESHOLD` consecutive
  failures the provider opens for `EV_CIRCUIT_COOLDOWN_SECONDS`, then allows a
  half-open probe. Open-circuit requests fast-fail with `CircuitOpenError`;
  the gateway returns `status:"degraded"` and records
  `envelope.metadata["degradation"] = {"kind":"circuit_open", …}` so the
  degradation is visible in the response envelope and the audit trail.
- **Cost cap**: `app/gateway/costs.py` projects each request against the
  current calendar-month spend from `model_calls` and refuses over-cap
  requests with `CostCapExceeded` before any provider call. The cap is
  `EV_MONTHLY_COST_CAP_USD` (default $40, matching
  `app.ops.budgets.MONTHLY_COST_BUDGET_USD`). Every completed call records its
  measured `cost_usd` in the audit envelope. Over-cap requests return a clear
  503/error and are never sent to the provider.

Memory-only paths (timeline, memories, audit, recall) never touch the provider
and stay fully functional when the API is unreachable — proven by
`test_memory_only_endpoints_work_with_provider_unreachable` with a blackholed
endpoint.

## 5. No local brain, no fallback brain (by owner direction)

The local chat provider was removed with the other retired brains: only
MiMo (reasoning) and Gemini Live (speech) remain, plus offline doubles
(`echo`, `mock`, `hash`) so the suite stays green with no keys. The
`EV_LOCAL_MODEL_*` endpoint settings stay in config for a future
self-hosted option, but no provider reads them. Memory features work
fully offline without any model.

## 6. Tool sandbox isolation

`app/tools/sandbox.py` selects the strongest available isolation and reports
it on every result (`isolation`, `network`, `memory_limit_mb`,
`process_limit`):

1. **seatbelt** (macOS): `sandbox-exec` with no network, host filesystem
   read/write denied except one scratch directory, plus hard rlimits and a
   live RSS memory watchdog. If the profile cannot be applied the call raises
   — it never silently downgrades.
2. **docker** (Linux/CI): `--network none --read-only --tmpfs /scratch` with
   memory/CPU/pids limits.
3. **process** (last resort): the original process jail; documented as NOT a
   security boundary.

`tests/test_tools_sandbox.py` contains 20 escape attempts — traversal,
absolute/symlink paths, cwd escapes, host reads/writes, HTTP and socket
egress, fork bomb, memory bomb, timeout, output cap, shell metacharacter
injection, environment exfiltration, workspace writes — all blocked.

The owner-facing tools execute endpoint accepts a fixed named operation from
app/tools/operations.py (currently workspace_smoke_test); raw command strings are rejected. It still passes
the R4 confirmation and access-log path before invoking the sandbox.

## 7. Web search with honest citations

`EV_SEARCH_PROVIDER=none` (default) means no key, no network, memory-only
research. `mock` is deterministic for tests. `brave` uses a user-supplied
Brave key. Unknown provider names fail closed. Results are normalized to
bounded `http(s)`-only URLs with numbered citations; every citation is a URL
the provider actually returned — EV never fabricates a source.

## 8. Boundary and tool integrity

Unchanged and still enforced:

- `validate_tool_calls` returns `ok` / `rectified` / `rejected` before dispatch;
- `guard_model_payload` short-circuits to `status="blocked"` with no provider
  call; credentials are redacted; `never_send_to_model` never crosses;
- `MAX_TOOL_ROUNDS = 3` caps the tool loop.

`ACTION_PERMISSIONS` was not touched; no Agent 14 dependency was created.

## 9. MiMo decision lane (typed choices)

`decide_via_role(state, questions, actor=...)` in `app/gateway/roles.py` is
the typed-decision seam. Questions are caller-owned `DecisionQuestion`s
(`choice` with a finite criteria map); the gateway validates the returned
choice against the declared enum and records the decision as a `GatewayCall`
so usage, latency, model, and answers stay auditable through
`log_model_call`. Providers that speak prose only (offline doubles) return
an error call instead of a fabricated choice.

The selected lanes stay separate: MiMo-V2.6-Flash for every non-speech
decision, including code, and Gemini Live 3.8 for the voice mouth
(Gemini-decides via `delegate_task`). Generative text the brain must not
invent (file bodies, invented list items) stays deterministic — the
decision picks the action, a local allowlisted handler executes it.

### Cross-owner migration dependencies

| Area | Remaining consumers | Fleet path owner / note |
| --- | --- | --- |
| EV task and surface adapters | `app/ev/{model_router,luna_adapter,desk_meaning,laptop_files,brain_file_runner,spark_act,spark_task,spark_look,spark_phone,diagnostics,look}.py` | Agent 15 ORACLE — **converted**: finite decisions are typed `DecisionQuestion`s (life task family/manner/focus, turn act, camera action, phone/Mac tool, desk act, turn route/operation, file-plan candidate); generative text stays deterministic, never invented |
| Visual task understanding | `app/ev/vision.py` | Agent 6 EYES — keep pixel perception local; MiMo sees raw pixels only through the explicit pixel gate |
| Speech pipeline | `app/voice/pipeline.py`, `app/voice/live/**` | Agent 4 VOICE — preserve ASR/TTS/realtime speech roles; Gemini decides, MiMo does medium-high work |
| Runtime / preflight / model status | `app/services/runtime.py`, `app/scripts/preflight.py` | Agents 14 / 20 — report the two-model topology, with active consent and remote-egress gates |
| Broader rollout contract | `docs/MIMO_ROLLOUT.md` | Agent 1 CONDUCTOR — reconcile the rollout document with the typed decision seam and current unsupported-task boundary |

## 10. Verification

```bash
cd backend
uv run pytest tests/test_gateway_api.py tests/test_gateway_unit.py \
  tests/test_gateway_streaming.py tests/test_tool_loop.py \
  tests/test_tools_sandbox.py tests/test_web_search.py \
  tests/test_search_citations.py tests/test_routing_gate.py \
  tests/test_mimo_provider.py -q
uv run python -m app.scripts.eval_gates --report eval/last-run.json
uv run ruff check app clients tests && uv run mypy app clients
```

MiMo lane (catalogue probe is key-free; provider smoke skips without a key):

```bash
cd backend
uv run python ../scripts/smoke_mimo.py     # exits 2 (SKIP) until the key lands
```

## 11. MiMo spoken response latency

The MiMo provider declares a per-instance `reasoning_effort` override. The
cognitive kernel's existing effort policy therefore reaches the outbound
request: compact conversation uses `low`, and file/computer/active work keeps
the configured work effort (`medium` by default). Non-kernel callers keep
`EV_MIMO_REASONING_EFFORT` unless they explicitly supply an override through
the role adapters. Previously the kernel's capability guard skipped the
undeclared attribute, so timing logs could say `low` while MiMo received the
global `high` value.

Low-effort requests use OpenRouter `provider.sort="latency"` to prioritize
first-token delay. Other requests retain `throughput` sorting and provider
fallbacks. These routing targets are described in the official
[OpenRouter provider routing documentation](https://openrouter.ai/docs/guides/routing/provider-selection).
The model, privacy gates, and speech personality are unchanged.

Offline gateway tests exercise actual kernel-to-provider HTTP payloads with
the global effort set to `high`: chat sends `low`, file work sends `medium`,
and a bare greeting never invokes MiMo even during active work. These checks
prove request policy and routing, not live provider or microphone-to-audio
latency. Production response times require an authorized deployment and a
measured voice session.

## 12. Realtime conversation with delegated MiMo execution

`EV_COGNITIVE_MODE=realtime_delegate` enables a conversational Realtime
front end and the `delegate_task` tool. Ordinary speech no longer waits for a
MiMo kernel turn. Explicit actions and substantial questions receive a durable
admission receipt; a separate asynchronous task executes the existing MiMo
kernel and returns its actual result to the originating live transport.

`app/cognitive/delegation.py` reuses `ResearchSession` rows (`mode=rt_delegate`),
without changing the database schema. Admission commits before returning;
server owner turn IDs deduplicate repeated tool calls. The worker receives the
canonical owner transcript, so model-generated tool arguments cannot invent
an owner confirmation. A task-local mode override selects MiMo without changing
shared settings or changing concurrent Realtime conversations. Original phone
lease/device authority is captured at admission and rechecked before execution
and each tool. Existing remote egress, payload boundary, and confirmation gates
remain in the tool pipeline.

One kernel worker runs at a time per process because its conversation ledger
is shared; admission allows up to eight pending tasks. Inference is bounded to
180 seconds. Coding runs are awaited inside the asynchronous worker rather than
returning an acknowledgement from a second untracked background task. Existing
long-lived goals are observed for up to five minutes and only reported as
verified completion when the goal runner records `COMPLETED_VERIFIED` evidence.
Approval requests and pending work remain `waiting`; a model answer is
`answered`, which does not assert that every requested external action occurred.

The additive master-authenticated `/v1/cognitive/delegations` endpoints submit,
list, inspect and cancel jobs. The same Realtime tool offers `status` and
`cancel` operations restricted to its original conversation. Disconnected
completion delivery cannot erase the saved result. Interrupted workers are
marked `interrupted` instead of automatically replaying possibly completed
side effects. Cancelling a worker cannot undo actions already performed.

Known limits: process-local callbacks require the live transport to reconnect
or query saved receipts; queued work is not automatically resumed after a
process restart; a goal still waiting after the observation window remains
available for review instead of receiving perpetual polling. Multi-process
workers should use a coordinated single executor to avoid concurrent writes to
the shared conversation ledger. Live latency still requires provider and
microphone measurements; admission timing is not task completion timing.
