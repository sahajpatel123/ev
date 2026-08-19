# opencode as an EV chat provider (optional fallback)

Typed chat uses official DeepSeek (`EV_CHAT_PROVIDER=deepseek`,
`deepseek-v4-flash`). Live talk uses OpenAI Realtime (`gpt-realtime-2.1-mini`)
when `EV_OPENAI_API_KEY` is set, else Grok Voice Think Fast 2.0 when
`EV_XAI_API_KEY` is set — those are not chat-completions models. See
`docs/VOICE.md` and `docs/ENVIRONMENT.md`.

`EV_CHAT_PROVIDER=opencode` remains an optional route through the locally
installed `opencode` CLI. It is not the voice default: the session API has no
native function calling, and `opencode-go` was slower and less reliable for
spoken turns than `api.deepseek.com`.

Owned files: `backend/app/gateway/opencode.py`,
`backend/app/scripts/opencode_agent_cost.py`,
`backend/tests/test_gateway_opencode.py`, `launchd/ev.opencode.plist`,
`.opencode/agents/ev-minimal.md`, this document. Shared files were appended to
inside `# --- AGENT OPENCODE ---` blocks only.

## 1. The transport is not OpenAI-compatible

The server exposes 162 paths and **zero `/v1` routes** — there is no
`/v1/chat/completions`. One turn is:

```text
POST /session                      {"title":"ev","model":{"providerID":"opencode-go","id":"deepseek-v4-flash"}}
POST /session/{id}/message         {"model":{"providerID":…,"modelID":…},"agent":"ev-minimal",
                                    "system":"<EV's own system prompt>","tools":{},
                                    "parts":[{"type":"text","text":"<user content>"}]}
DELETE /session/{id}
```

The reply text is the `parts` entries with `type == "text"` (`reasoning` parts
are deliberately **not** treated as user-facing text). Tokens and cost come
from `body.info` and are mapped as:

| EV usage key | opencode source |
| --- | --- |
| `prompt_tokens` | `tokens.input + tokens.cache.read + tokens.cache.write` |
| `completion_tokens` | `tokens.output + tokens.reasoning` |
| `total_tokens` | `tokens.total` |
| `cached_prompt_tokens` | `tokens.cache.read` |
| `cost_usd` (+ `cost_source: opencode_reported`) | `info.cost` — opencode's own measured spend |

## 2. The preamble tax and EV's minimal agent

Every built-in opencode agent injects a coding preamble into each request. All
numbers below are opencode's own reported figures for the identical prompt
`"say EV_OPENCODE_OK"` with a 60-character system prompt
(`make opencode-agent-cost`):

| agent | prompt tokens | cost / call (cold) | cost / call (prompt-cache warm) |
| --- | --- | --- | --- |
| `ev-minimal` (EV's own) | **223** (95 + 128 cached) | $0.0000216 | $0.0000096 |
| `plan` | 14,666 | $0.0010343 | $0.0000346 |
| `general` | 13,345 | $0.0006926 | $0.0000236 |
| `build` (default) | 14,386 | $0.0010100 | $0.0000244 |

`ev-minimal` is ~14.4k prompt tokens cheaper per request than `plan` — 48×
cheaper per cold call. It is defined at `.opencode/agents/ev-minimal.md` (a
project-level opencode agent, picked up because the server's working directory
is the repo root) with every permission denied, no tools, and a one-line
prompt; EV supplies its own system prompt per request.

Changing the agent requires restarting the server:
`launchctl kickstart -k gui/$UID/ev.opencode`.

## 3. Session lifecycle

opencode sessions accumulate history, which would both corrupt EV's own
single-thread conversation state and inflate cost without bound. EV therefore
creates a **fresh ephemeral session per request and deletes it in a `finally`**
(`DELETE /session/{id}`), verified by tests and by `GET /session` returning an
empty list after live runs. `EV_OPENCODE_SESSION_REUSE=true` opts into a sticky
session; it logs a warning at creation because opencode then holds
conversation state EV does not manage.

One artifact does persist: opencode keeps a single 160 KB git snapshot
directory per project under `~/.local/share/opencode/snapshot/<project-hash>/`.
It is per project, not per session, and does not grow per request.

## 4. Tool calling: option (a), declared unsupported

The session API accepts **no** OpenAI-style function definitions, so
`OpenCodeProvider.supports_tools = False` and `chat_with_tools`:

1. answers without tools,
2. logs a warning naming the number of tools dropped,
3. sets `usage["degraded"] = True` and
   `usage["degradation"] = {"kind": "tools_unsupported", …}`.

Consequence for EV: the model cannot start tools itself. Chat therefore runs
**execute-then-word** (`backend/app/ev/turn.py`):

1. Snapshot WORKING ON (this request, thread focus, current task/project).
2. Prefetch read intelligence (weather, math, memory, …).
3. Dispatch write/life actions the owner asked for (`plan_life_tool_calls` →
   `dispatch`) *before* the LLM speaks.
4. One wording call to opencode-go / deepseek-v4-flash with identity,
   WORKING ON, action receipts, and the briefing.
5. If the model describes an action instead of confirming it, replace the
   reply with `life_success_reply` from the real receipt.

Native tool-calling providers still use the bounded tool loop after those
pre-dispatched writes (already-run names are skipped). Memory retrieval into
the request envelope is unchanged.

Option (b) is implemented but **off by default**
(`EV_OPENCODE_TOOL_EMULATION=true`): EV describes the tools in the system
prompt and requests structured output via the server's
`format: {"type":"json_schema", …}` field, then parses `tool_calls` into
`ToolCall` objects. Nothing bypasses validation — the gateway's existing
`validate_tool_calls` runs on the result exactly as for DeepSeek. It is off by
default because the model does not honour the schema reliably: the first live
probe returned `{"tool_calls":[{"name":"search_decisions","query":"gym membership"}]}`
— arguments inlined, `reply` missing. EV normalises that shape deterministically
and records `usage["tool_emulation_problem"]`, but a provider that needs
shape repair on its first attempt is not a foundation for EV's tool loop.

## 5. Streaming is real, not faked

`stream_chat` opens `GET /event` (SSE), then posts
`/session/{id}/prompt_async`, and forwards `message.part.delta` events whose
`partID` belongs to a **text** part — reasoning deltas are filtered out. The
terminal chunk carries opencode's reported tokens/cost. If opencode reports no
totals, the terminal chunk is marked
`degradation: {"kind": "usage_missing"}` instead of pretending the call was
free. Cancelling the generator closes the SSE stream, cancels the prompt task
and disposes the session.

## 6. Reliability and fail-closed behaviour

Reuses EV's existing seams: `CIRCUIT_BREAKERS.get("opencode")`,
`http_timeout()`, `max_attempts()`/`wait_for_retry()`/`is_transient()`.
Deviation from `DeepSeekProvider`, deliberately: the prompt POST does **not**
retry, because a read timeout may leave a billed generation running
server-side and a retry would pay for it twice. Session creation and health
checks do retry.

Every failure names its remediation, e.g.

```text
opencode provider unavailable at http://localhost:4096: health check failed …
(api key visible to EV: not found). start the opencode server:
`launchctl kickstart -k gui/$UID/ev.opencode` (installed by
launchd/ev.opencode.plist) or, in the foreground,
`opencode serve --hostname 127.0.0.1 --port 4096`. Also: OPENCODE_API_KEY is
not set where EV or the opencode server can see it …
```

`make preflight` reports the `chat` row as REAL only when the server answers
`/global/health` **and** a credential is visible; otherwise PARTIAL with the
same remediation.

## 7. Running the server

```bash
make opencode-up        # installs launchd/ev.opencode.plist as a LaunchAgent
make opencode-status    # launchd state + health + live session list
make opencode-down
```

launchd does **not** read `~/.zshrc`, so an `OPENCODE_API_KEY` exported there is
invisible to the job. The plist sources EV's `.env` and then
`~/.config/ev/opencode.env`, and exits 78 with an explicit log line if neither
provides the key.

**Dependency note.** `launchd/install.sh`, `launchd/uninstall.sh` (Agent 14
PULSE) and `brew/setup.sh` (Agent 20 LAUNCH) enumerate services as
`api worker scheduler runtime ears collector`. Adding `opencode` to those three
lists would put the server under `make native-up` / `make native-down`; those
files are not mine to edit, so `make opencode-up` / `make opencode-down` cover
it in the meantime.

## 8. Cost meter caveat

`log_model_call` derives `cost_usd` from `app.ops.budgets.MODEL_PRICES_USD_PER_1M`,
which has no `opencode` row, so the audit ledger prices these calls at the
`default` rate ($1.00/$3.00 per 1M) rather than opencode's reported cost. That
over-states spend (the cap trips early — safe direction) but it is not the real
number. The provider already carries the real figure in
`usage["cost_usd"]`/`cost_source`. **Dependency note for the owner of
`app/ops/budgets.py`:** add `"opencode": {"input": 0.27, "output": 1.10}` (or
wire `usage["cost_usd"]` through `log_model_call`) to make the ledger exact.

## 9. Verification

```bash
cd backend
uv run pytest tests/test_gateway_opencode.py -q
uv run pytest -q
uv run ruff check app clients tests && uv run mypy app clients
uv run python -m app.scripts.eval_gates --report eval/last-run.json
cd .. && make preflight && make opencode-agent-cost
```
