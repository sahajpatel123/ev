# Agent Runtime — arbitrary goals, executed locally with receipts

The agent runtime is Evie's doer: one goal in, a model-planned sequence of real
tool calls out (shell, files, web, apps), every result observed before the next
decision, and an honest stop when a step fails or a budget runs out.

It is additive to the kernel. The kernel stays the conversational mind and the
owner of the narrow semantic capabilities; the agent is the escape hatch for
goals no single capability covers. A conversational turn hands work over with
the `agent.run` capability; the surfaces are listed below.

## Surfaces

| Surface | Command |
| --- | --- |
| Local (no server) | `python -m app.agent "goal"` (from `backend/`) |
| Make target | `make agent GOAL="goal"` / `make agent YES=1 GOAL="goal"` |
| HTTP API | `POST /v1/agent/runs`, `GET /v1/agent/runs/{id}`, `POST /v1/agent/runs/{id}/decision`, `POST /v1/agent/runs/{id}/cancel`, `GET /v1/agent/runs/{id}/events` (SSE), `GET /v1/agent/brain` |
| `ev` CLI (server) | `ev agent "goal"`, `ev agent tasks` / `status`, `ev agent approve <task-id>`, `ev agent cancel <task-id>` |
| HTTP tasks | `GET /v1/agent/tasks`, `GET /v1/agent/tasks/{id}`, `POST /v1/agent/tasks/{id}/decision`, `POST /v1/agent/tasks/{id}/cancel` |
| Kernel hand-off | MiMo calls the `agent.run` semantic tool; the bridge starts the run and reports the receipt. Voice turns get an immediate ack (1.5s probe), typed turns wait up to `EV_COGNITIVE_WORK_TIMEOUT_SECONDS`, and the finish lands as a notification |

Local runs need no secrets manager and no server: the CLI primes the process
environment from the repo `.env` then `~/.ev/secrets/production.env` (existing
exports always win) and runs in-process.

## Brain resolution

`EV_AGENT_PROVIDER` (or `--provider`, or the API body) selects the brain:

```text
auto | xai | deepseek | openai | mimo | local | echo
```

`auto` walks `deepseek -> openrouter -> xai -> deepseek-direct -> openai ->
mimo -> local` and takes the first provider whose key is present. The main
brain is **DeepSeek V4.1 Flash on OpenRouter**
(`deepseek/deepseek-v4.1-flash`, 1M context, native tool calls) through the
`deepseek` provider; `openrouter` is the generic alias and `deepseek-direct`
is the direct-API fallback. Speech stays Gemini Live; MiMo remains an explicit
fallback. `echo` is the offline double and is **refused**
as an agent brain: with no usable brain the run returns `brain_unavailable`
with instructions instead of pretending to think. Resolution is network-free;
a stale key fails at call time with the provider's own error, mapped to
`brain_unavailable` / `egress_denied` on the receipt.

Cloud brains are OpenAI-compatible chat providers registered alongside `mimo`
(`app/gateway/cloud_providers.py`). They respect the existing
`remote_processing_allowed("chat_egress")` gate.

## Tools

| Tool | Policy | Notes |
| --- | --- | --- |
| `run_shell` | classified | One-shot, bounded; read-only verbs AUTO, build/test journaled, destructive CONFIRM, hard DENY final |
| `read_file` / `list_dir` / `search_files` | read | Inside the workspace root by default; outside root parks for approval; secret paths hard-denied |
| `write_file` | confirm | Create/overwrite, verified by read-back (sha256); 1 MiB cap |
| `append_file` | confirm | Append (creates if missing), read back + hash |
| `edit_file` | confirm | Find/replace with a count; fails without changing anything when find is absent |
| `move_path` / `copy_path` | confirm | Rename/move or copy, never overwriting; destination sha256 returned |
| `delete_path` | confirm | Moves to the workspace trash with an undo record |
| `sql_query` | confirm for writes | Local SQLite SELECT/DML/DDL; writes transactional and approval-gated, dangerous SQL DENY |
| `fetch_url` | read | SSRF guard refuses loopback/private/reserved hosts and unsafe redirects; bounded bytes |
| `search_web` | read | Existing search-provider registry; refuses honestly when unconfigured |
| `open_app` / `open_url` | confirm | macOS `open`; URL schemes restricted to http(s) |
| `observe_app` | read | Bounded accessibility tree of one app's front window, with a frame id |
| `act_in_app` | confirm | One bounded AX action (click / set value / keystroke); secure fields always refused |
| `verify_app` | read | Re-observes and checks text is present/absent; verified only on a real screen match |
| `press_key` | confirm | One named key (return/tab/escape/arrows/...) with modifiers, through Evie.app |
| `run_in_terminal` | confirm | Focus a terminal app, type one command, press Return, optionally verify on screen |
| `terminal_open` | confirm | Open a real Ghostty window with tmux-backed, addressable panes and run each command |
| `terminal_run` / `terminal_read` / `terminal_list` | mixed | Type into a tracked pane and read back its visible output; list live sessions |
| `capture_screen` | read | Screenshot through Evie.app (TCC) + OCR/vision description |
| `read_image` | read | OCR + vision description of any local image (screenshots, photos, documents) |
| `run_applescript` | confirm | One AppleScript in a scriptable app; privilege escalation refused |
| **the capability graph** (42 tools) | per capability | One typed tool per semantic capability: `memory.search`, `life.mail`, `life.messages`, `life.send`, `people.lookup`, `weather.get`, `files.act`, `code.act`, `digital.act`, `goal.*`, `timer.act`, `pim.act`, `capability.discover`, `atlas.discover`, `digital.discover`, … See "The capability graph is the tool surface" below |
| `todo_write` | control | The model's own visible plan for the run; published on the run's event stream |
| `finish` / `ask_owner` | control | End the run with a summary, or stop and ask one question (`ask_owner` accepts `options` for a multiple choice) |

Containment, secret-path denial, and command classification come from the
existing fabric lanes (`app/fabric/files.py`, `app/fabric/shell_sessions.py`).
The agent never reimplements policy and never overrides a lane DENY.

`EV_AGENT_AUTONOMY=trusted` (default) executes mutating steps without a second
prompt — hard DENY (sudo, secret paths, `rm -rf /`) is still final.
`confirm` parks every mutating step instead.

## The capability graph is the tool surface

Until 2026-10-21 the agent lane reached personal capabilities through one
generic tool that dispatched by **name string**:

```python
mac_call(name="life.send", arguments={"to": ..., "text": ...})
```

That shape had three faults, all visible in the working tree:

1. **No typed parameters.** The model only saw a one-line description of a
   capability, so it had to guess that `life.send` wanted `to` and `text`
   while `files.act` wanted `op` and `path`. Wrong guesses surfaced as
   executor failures, not as schema errors.
2. **A hand-maintained allowlist.** `mac_call` carried a hardcoded list of ten
   read-only capability names it would run without asking. A new capability
   had to be added to that Python list, or it silently started asking for
   approval on a plain read.
3. **The discovery tool could not be discovered.** `capability.discover` — the
   one tool that answers "what can I do?" — was explicitly refused by
   `mac_call`.

Now each of the 44 semantic capabilities is **its own typed tool**, with
schemas read from `app/cognitive.capabilities.SEMANTIC_TOOLS` and execution
dispatched to `app.cognitive.executor.execute_semantic`
(`backend/app/agent/capability_bridge.py`). `mac_call` and `mac_capabilities`
are **removed**: with every capability typed, a name-string dispatcher is
strictly worse — no schema, and a second allowlist to maintain.

The model's surface went from 31 tools to **71**: 42 semantic capabilities
(one each; `agent.run` is excluded because it owns the lane), 28 hands and
control tools, and `finish` / `ask_owner`. Adding a capability is now a row
in `SEMANTIC_TOOLS` — a data edit, not a Python edit.

Both reference harnesses this workspace reverse-engineered work the same way:
Grok exposes 26 typed schemas plus `search_tool`/`use_tool` for runtime
discovery, and OpenBot keeps one row per provider in
`packages/contracts/src/agent-providers.ts` instead of twenty hand-written
copies. See [`RE_GROKBOT_OPENBOT.md`](RE_GROKBOT_OPENBOT.md).

### Permission is read off the capability's own row

`_classify` derives the fabric action class from each tool's own `permission`
(`messaging:send` → `send`, `fabric:shell` → `shell`) rather than matching on
tool names. A capability whose **operation** is a read is AUTO:

| Capability | Read operation | Effect operation |
| --- | --- | --- |
| `owner.profile` | `op=get` | `op=set` |
| `files.act` | `op=read` / `op=list` / … | `op=write` / `op=delete` / … |
| `digital.act` | `service=gmail, operation=search` | `service=gmail, operation=send` |
| `shell.session` | `op=stream` / `op=tail` / `op=list` | `op=start` / `op=send_input` |
| `terminal.layout` | `op=plan` / `op=list` / `op=read` | `op=save` / `op=open` / `op=close` |

The operation vocabularies are **imported from the owning lanes**
(`app.ev.file_sandbox.READ_OPS`, `app.digital.tools._GMAIL_READ_OPS`) rather
than copied, so there is one list per lane. A capability whose operation is
not in its read set is an effect and asks. A secret **path** is still DENYed,
and the DENY check runs before anything that would shortcut to AUTO.

### The model keeps a plan, and can offer a choice

`todo_write` records the run's visible plan and publishes it on the run's
event stream, so the owner can watch the shape of the work while it runs.
(Grok's harness has the same tool; OpenBot had to switch the Claude SDK's
plan tools on explicitly to get the behaviour.)

`ask_owner` accepts `options`, so the model can offer the owner a choice
between known paths instead of an open question. The options travel with the
`needs_input` result.

### A long run stays inside the context window

`backend/app/agent/compaction.py`. Grok's harness compacts at
`session.auto_compact_threshold_percent = 85`; Evie compacts at
`agent_compact_threshold_percent` (default 85) of `agent_context_tokens`,
counting the tool surface because it is sent on every request.

Compaction is **factual**. The system prompt, the owner's goal, and the
model's own turns are kept verbatim; a compacted tool result becomes a
receipt digest that still says whether the call succeeded. Nothing is
summarised by a model, because a generated summary can state something the
tool never returned. A digest that would be longer than the receipt it
replaces is discarded rather than substituted.

### Proven local loop (no cloud computer)

The reference flow was run on this Mac:

```text
screenshot (Ghostty sessions)
  -> read_image: Apple Vision OCR found "opencode resume --session demo42"
  -> terminal_open: real Ghostty window, tmux-backed, addressable pane
  -> terminal_run: typed the command, pressed Return
  -> terminal_read: saw "RESUMED-demo42" and the new prompt
  -> finish: "the terminal showed RESUMED-demo42"
```

That is the GrokBot shape: the model plans, the tools act on this machine, and
the receipt is read back before anything is claimed.

## Voice topology: Gemini speaks, DeepSeek acts

- **Ears/mouth:** Gemini 3.8 Live extended thinking (speech only).
- **Main brain:** DeepSeek V4.1 Flash on OpenRouter (`deepseek/deepseek-v4.1-flash`), with native tool calls.
- **Hands:** the durable local agent (terminal, screen, files, apps, mail, memory).

Gemini is told what the system is, what it can hand over, and how: it calls
`delegate_task` with the owner's exact request, constraints, desired end state,
and verification. The voice delegate runs local work through the durable agent
lane directly (not the old computer executor), and returns the real result.
Its tool contract forbids "sent to the backend / submitted / queued / in the
background / I'll tell you when it's done"; the only acknowledgement is a short
"On it." while the verified result arrives as its own turn.

## Durable task lane — why "submitted, I'll inform you" can no longer happen

A delegated goal has a ledger row **before** any runner starts, and the spoken
line is derived from that row, not from a promise:

```text
goal -> ledger row (SQLite, WAL) -> live agent run -> mirror every step
     -> completed / failed / cancelled / interrupted
```

- **Typed chat** waits inline (`EV_AGENT_CHAT_INLINE_SECONDS`, default 30s).
  If the task finishes, the reply is the real verified summary. If not, the
  reply is the real last step plus the task id — never a bare acknowledgement.
- **Voice / phone** answer in ~1.5s with the real first step, then a
  completion notification carries the final receipt.
- **Status is deterministic**: "what are you doing", "status", "progress" read
  the ledger directly, with no model call and no way to invent progress.
- **Approval is conversational**: a parked task asks "say yes to continue or
  no to stop". The next yes/no resumes or denies it before any other handler
  (including a parked send) can swallow the answer.
- **Cancel is real**: "stop/cancel that" cancels the live run and marks the row
  cancelled, including from the older reflex path.
- **Restart honesty**: on boot the watchdog marks rows owned by the previous
  process `interrupted` ("the process running this task stopped") instead of
  leaving them "running" forever. `RETRY` is a new goal, not a phantom resume.
- **Both pipelines, one lane**: the intake is wired into the MiMo kernel *and*
  the legacy kernel-off chat pipeline (`EV_COGNITIVE_MODE=realtime_delegate`),
  so the production topology gets the same guarantee without changing its
  architecture.
- **No unbacked promise survives a turn**: if a model replies "submitted /
  queued / I'll inform you" and no task row was created during that turn, the
  goal is forced into the durable lane and the real result (or real approval
  question) replaces the claim.
- **Routing order**: task intake runs before the static explain/file/code
  routers, so a compound goal ("find every file…, read them, and summarize")
  cannot be hijacked into an explain answer. Simple known capabilities
  (weather, timers, messages, opening apps, parsed single file asks) keep their
  fast paths. Compound software goals go to the durable agent; explicit
  overnight/studio language ("keep working on it overnight", "steer the coding
  job") keeps the legacy code studio, which is the better engine for multi-shift
  scaffolds. The old architecture is still in the workspace; the new lane is
  the guarantee for general work.

## The loop

```text
goal + system contract
  -> brain decides (native tool calls)
    -> validate arguments against the tool schema
      -> classify policy: AUTO | CONFIRM | DENY
        -> AUTO: execute; CONFIRM: one owner decision; DENY: refuse
          -> append the receipt as a tool message
            -> repeat until finish / ask_owner / budget / cancel / brain failure
```

Every provider-bound payload passes `app/security/boundary.py`
(`guard_model_payload`): `never_send_to_model` content blocks the call
(`privacy_boundary`), and credential-shaped strings in tool output are redacted
before the brain sees them.

Budgets: `EV_AGENT_MAX_STEPS` (model turns), `EV_AGENT_MAX_TOOL_CALLS`,
`EV_AGENT_MAX_SECONDS`. Cancellation is checked between every step. A `finish`
that claims success while any executed step failed is downgraded on the
receipt (`claimed success but steps failed`) — the model cannot talk its way
past a failed receipt.

## Approval model

- Read-only tools run immediately.
- Mutating tools park for one decision: **approve this call**, **approve the
  whole run**, or **deny**. A denial is returned to the brain as a failed
  observation so it can adapt, not silently retry.
- Outside-root reads park the same way when the lane reports `needs_confirm`.
- Hard DENY (sudo, `rm -rf /`, secret paths, decode-pipe-to-shell) is final at
  every approval level, including `--yes` and
  `EV_AGENT_ALLOW_DESTRUCTIVE=true`.
- A kernel hand-off whose turn carried untrusted content (screenshot, email,
  web page) is refused (`POLICY_BLOCKED`): injected text never starts a
  shell-capable run.
- Local CLI: interactive on a TTY; denies in a pipe unless `--yes`.
- HTTP: the run parks, `GET /runs/{id}` shows `awaiting_approval` with the
  pending request, and `POST /runs/{id}/decision` answers it.
- Kernel hand-off: the bridge notifies the owner (live voice + notification
  backends) with the run id when a step needs approval; the run waits up to
  `EV_AGENT_APPROVAL_TIMEOUT_S` before it stops and says so.

## Receipts and traces

Every run returns goal, brain, root, approval mode, every step
(tool/arguments/policy/approval/ok/verified/error/result), tool-call count,
token usage, and the final summary. Finished runs are written to
`EV_AGENT_TRACE_DIR` (default `~/.ev/agent/runs/<run-id>.json`) atomically;
file bodies are not written into the trace (their sha256 is on the receipt),
and shell output is redacted by the shell lane before it reaches a receipt.
The persisted step record additionally turns file/web bodies into length +
preview, drops `write_file` bodies, and redacts token-shaped argument values,
so a trace is safe to keep even when a goal mentions a credential.

## Verification

```sh
cd backend
uv run pytest tests/test_agent_runtime.py -q          # 38 hermetic tests
uv run pytest tests/test_agent_tasks.py -q            # 18 task-lane tests
uv run pytest tests/test_terminal_continuity.py -q   # 12 screenshot->Ghostty tests
uv run pytest tests/test_agent_intake_local.py -q    # 4 local-route tests
uv run python -m app.agent --brains                   # which brains resolve
uv run python -m app.agent --root /tmp/demo "list what is here"
```

The hermetic suite uses a scripted provider: no network, no keys. It pins the
loop contract, the approval paths, hard DENY, budgets, cancellation, secret
paths, the SSRF guard, the kernel bridge's honest offline failure, and the
HTTP surface.

## Local hands: the Mac is the executor

The agent is not limited to a hidden shell. Each tool below is a real adapter
with a measured receipt, and the loop offers all of them to the brain:

| Tool | What it does | Proof in the receipt |
| --- | --- | --- |
| `terminal_open` / `terminal_run` / `terminal_read` / `terminal_list` | Opens Ghostty (tmux-backed, one pane per command or attaches a live tmux session), types a command, and reads the pane back. | `terminal_id`, `pane_id`, captured output |
| `capture_screen` | Captures through Evie.app's TCC bridge, falls back to `screencapture`, then describes and OCRs the image. | `path`, `source`, `description`, `ocr` |
| `read_image` | Apple Vision OCR plus the configured multimodal brain, for screenshots, photos, and documents. | `text`, `ocr`, `provider`, `degraded` |
| `run_applescript` | One bounded AppleScript for Mail, Messages, Finder, Music, or any scriptable app. Escalation is refused. | `stdout`, exit code |
| `mac_capabilities` / `mac_call` | The kernel's 44 semantic capabilities (mail, messages, calendar, contacts, weather, timers, PIM, digital ops) through one executor. | the executor's own result |
| `append_file` / `edit_file` / `move_path` / `copy_path` / `sql_query` | Surgical file DML and local SQLite DDL/DML. | read-back, sha256, replacement count, rows |

**Routing.** `app/cognitive/local_route.py` recognises machine work
(files, terminal, apps, screen, sessions) before any model call. The kernel
hands those turns to the durable agent instead of MiMo's four-round
conversational budget; when a model turn still exhausts its tools, the kernel
gives the agent one recovery attempt rather than saying "still working".

**Autonomy.** `EV_AGENT_AUTONOMY=trusted` (default) executes the owner's own
authenticated turn without parking every write. Hard DENY rules — `sudo`,
secret paths, `rm -rf /`, privilege escalation — are never grantable.
`EV_AGENT_AUTONOMY=confirm` restores one approval per mutating action.

**Phone as a remote.** A trusted phone's machine work runs on the Home Station
through `intake.handle_task`, not through the broker queue. A fast task returns
its real result in the HTTP reply; a longer one returns progress and pushes the
finished summary to the phone's device inbox (`task_finished` / `task_stopped`).

**Voice.** In `realtime_delegate` mode the live model delegates machine work,
and local work skips the research graph so the agent starts immediately. The
spoken acknowledgement is one human line ("Okay — doing that now"); the
verified result is spoken by the live session when the run finishes.

## Model-first routing

`handle_task` no longer lets the static verb/noun regexes decide alone.
`route_goal()` (`app/agent/intake.py`) asks the agent brain to classify every
turn the regexes would drop, so novel phrasings reach the thinker instead of
falling through to legacy paths. The regexes stay as the offline fallback —
no usable brain, timeout, crash, unreadable reply, or privacy-boundary
refusal all degrade to yesterday's verdict, never to a dropped turn or an
invented answer. `EV_AGENT_ROUTING`: `model_backstop` (default, instant for
known task shapes) | `model_first` (brain sees every turn) | `static`
(legacy only). Budgets: `EV_AGENT_ROUTE_TIMEOUT_S` (8s),
`EV_AGENT_ROUTE_VOICE_TIMEOUT_S` (4s). Hermetic suites never route through
ambient keys; tests inject the router provider.

## WHAT IS STILL NOT REAL

Named honestly, in the repo's convention:

- **The harness runs in-process.** Grok's harness is a separate program
  speaking ACP over stdio; OpenBot's is a separate CLI; Evie's loop runs
  inside the API worker. A hard crash in a native library still takes the turn
  with it. A supervised child process is the next architectural step.
- **No runtime tool discovery.** Grok's `search_tool`/`use_tool` let the model
  find a capability that was not in its schema at all. Evie's
  `capability.discover` does the same *job* — it returns the capability names
  for a domain — but every capability is still present in the prompt.
- **GUI driving depends on the Evie.app bridge.** `observe_app` / `act_in_app` /
  `verify_app` drive the existing AX operator, so they only work while the
  signed app and its bridge are running. Vision is read-only
  (`capture_screen` / `read_image`): the agent sees the screen, but clicking is
  AX-grounded, has no drag/scroll gesture tool, and reads only the front window.
- **Terminal sessions are not resurrected.** `terminal_open` creates real tmux
  sessions behind visible Ghostty windows and `terminal_run` reads each pane
  back; a reboot kills them. The agent reopens a session, it does not replay
  the command history.
- **No per-run cost cap.** Token usage is recorded on every receipt but the
  loop does not stop on a dollar threshold (the global gateway cost cap still
  applies to the same provider keys).
- **No mid-run provider failover.** A brain failure stops the run
  (`brain_unavailable`) rather than resuming on the next configured provider.
- **No HUD approval button yet.** Chat/voice "yes" works, and
  `ev agent approve <task-id>` / the HTTP decision endpoint work for a parked
  task; the macOS HUD does not render the button yet.
- **Interrupted tasks are not auto-resumed.** After a restart the ledger says
  `interrupted`; the owner has to ask again (honest, not a phantom resume).
- **The legacy code studio keeps its own job store.** Its active job is included
  in "status", but studio/intern jobs are not ledger rows with their own
  approval ids yet; only the general agent lane has that. Explicit overnight
  goals still use that lane by design.
- **Non-macOS app control.** Shell/files/web work anywhere; `open_app` is
  macOS-only and says so elsewhere.
- **One run per goal.** There is no dependency graph or parallel sub-goal
  execution; the sequential loop is deliberate for auditability.
