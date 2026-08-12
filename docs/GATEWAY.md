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

Implemented by `echo`, `mock`, `deepseek`, and `local`. DeepSeek/local use the
OpenAI-compatible `stream: true` chat-completions endpoint and parse SSE lines
into `ChatStreamChunk` deltas; tool-call deltas are accumulated per index and
returned on the terminal chunk.

### SSE endpoint

`POST /v1/gateway/stream` — body is the same `GatewayChatRequest` shape as
`POST /v1/gateway/chat`:

```text
event: delta
data: {"text":"…","final":false}

event: done
data: {"request_id":"…","provider":"deepseek","model":"…",
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

## 2. Routing: single-provider honest, still fail-closed

DeepSeek is the primary (and currently the only) reasoning provider, so
multi-provider routing is a no-op. `app/gateway/routing.py` and
`scripts/routing_gate.py` state that plainly:

| Situation | Gate result | Selection reason |
| --- | --- | --- |
| One provider configured | `routing_is_noop` fails closed (`passed=False`) | `single_provider_routing_noop` |
| Two providers + no evidence | fails closed (volume) | `configured_fail_closed_no_evidence` |
| Two providers + unhealthy evidence | fails closed (health/latency) | `routing_gate_failed_fail_closed` |
| Two providers + gate passes + cheap/privacy mode | passes → `local` | `cheap_privacy_sensitive_routed_local` |
| Two providers + gate passes + hard mode | passes → `deepseek` | `hard_reasoning_routed_deepseek` |

The gate never reports a meaningless pass: with fewer than two configured
candidates it fails closed with an explicit no-op check. Every call records
the winning provider, reason, and evidence in
`envelope.metadata["provider_selection"]`.

Unknown provider names (`EV_CHAT_PROVIDER` not in the registry) raise
`UnknownProviderError` and log at error level — no silent echo fallback.

## 3. API-only reliability (timeouts, retry, circuit breaker, cost cap)

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

## 4. Optional future local brain (preserved, not required)

The local provider stays registered and functional for a future self-hosted
model, but nothing requires it: DeepSeek is the default narrative and memory
features work fully offline without any model.

Human action if a self-hosted brain is ever enabled (weights are not
downloaded by CI):

```bash
ollama pull qwen3:1.7b
# EV_LOCAL_MODEL_NAME=qwen3:1.7b
# EV_LOCAL_MODEL_BASE_URL=http://localhost:11434/v1
```

Why Ollama over MLX-LM for that future option: Ollama already runs
llama.cpp/MLX backends on macOS, exposes the OpenAI-compatible API we already
speak, and keeps the model server as a separate process. MLX-LM is a fine pure
Apple alternative but adds a second runtime and no OpenAI-compatible server
without extra glue. The brain is registered in `backend/app/ml/registry.py` as
`exclusive` (Qwen3-1.7B Q4, 1000 MB resident / 1100 MB disk), evicting
on-demand models and taking the arbiter lock; 165 + 1000 = 1165 MB is well
under the 2400 MB ceiling.

## 5. Tool sandbox isolation

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

## 6. Web search with honest citations

`EV_SEARCH_PROVIDER=none` (default) means no key, no network, memory-only
research. `mock` is deterministic for tests. `brave` uses a user-supplied
Brave key. Unknown provider names fail closed. Results are normalized to
bounded `http(s)`-only URLs with numbered citations; every citation is a URL
the provider actually returned — EV never fabricates a source.

## 7. Boundary and tool integrity

Unchanged and still enforced:

- `validate_tool_calls` returns `ok` / `rectified` / `rejected` before dispatch;
- `guard_model_payload` short-circuits to `status="blocked"` with no provider
  call; credentials are redacted; `never_send_to_model` never crosses;
- `MAX_TOOL_ROUNDS = 3` caps the tool loop.

`ACTION_PERMISSIONS` was not touched; no Agent 14 dependency was created.

## 8. Verification

```bash
cd backend
uv run pytest tests/test_gateway_api.py tests/test_gateway_unit.py \
  tests/test_gateway_streaming.py tests/test_tool_loop.py \
  tests/test_tools_sandbox.py tests/test_web_search.py \
  tests/test_search_citations.py tests/test_routing_gate.py \
  tests/test_local_model_provider.py -q
uv run python -m app.scripts.eval_gates --report eval/last-run.json
uv run ruff check app clients tests && uv run mypy app clients
```
