# Reverse engineering: Grok Bot and OpenBot → Evie's harness architecture

**Date:** 2026-10-21 (first pass) · 2026-10-21 (deep pass, this document)
**Targets:**
- `/Applications/Grok Bot.app` — `sand` / `Grok Bot` v0.71.2, Electron, Cursor-derived
- `/opt/homebrew/lib/node_modules/@xai-official/grok` — `@xai-official/grok` v1.0.62, the harness the app drives
- `/Applications/OpenBot.app` — `openbot` v0.46.0, Electron
- `nightly-labs/openbot` at `6ed78f8` — the source-available repository
**Purpose:** interoperability and architecture study of software the owner runs,
to redesign Evie's turn handling. No license or DRM circumvention; no proprietary
code was copied. Every change in Evie is Evie's own Python.

> **Revision note.** The first pass of this document concluded that "the model
> loop lives in the harness" and marked it *inferred*, because it had only read
> the app bundle. The deep pass found the harness itself on disk and read its
> actual model-facing contract. The conclusions did not change; the evidence
> did. Where the first pass said *inferred*, this document names the file and
> the line.

---

## 1. Method

- `triage.py` and `app_inspect.py` on both app bundles (magic bytes, app
  markers, sizes, notable files) — output kept in `~/Code/ev-re/notes/`.
- `npx @electron/asar extract` into `~/Code/ev-re/extract/{grokbot,openbot}`;
  all analysis happened outside the app bundles.
- Targeted `grep -o` on minified bundles, `package.json` inspection, and
  source reading of the cloned OpenBot repository.
- **The decisive step:** the harness is not inside the app. It is a separate
  installed program on this machine, with a live session directory that
  contains the exact system prompt and the exact tool schemas the model was
  given. Reading those is stronger than any amount of bundle archaeology.

```bash
python3 -I ~/.agents/skills/reverse-engineering/scripts/triage.py "/Applications/Grok Bot.app"
npx @electron/asar extract "/Applications/Grok Bot.app/Contents/Resources/app.asar" ~/Code/ev-re/extract/grokbot/app
ls ~/.grok/sessions/%2FUsers%2Fsahajpatel%2FCode%2Fev/
```

---

## 2. Grok Bot (v0.71.2) — the app is a client

`package.json` identifies the product as `sand` / `Grok Bot` v0.71.2, author
`SpaceXAI`, homepage `cursor.com`, depending on the `@anysphere/*` packages
(`agent`, `agent-core`, `agent-exec`, `shell-exec`, `local-exec`,
`grok-bot-harness`, `voice-call-harness`). It is a Cursor-derived Electron
app, not a standalone model client.

Layout observed:

```
dist/electron-main/main.cjs           small launcher
dist/electron-main/main-app.cjs       main process (2.6 MB)
dist/local-exec-daemon/main.cjs       privileged local execution service (3.5 MB)
dist/node-agent-coordinator/main.cjs  agent coordination (893 KB)
dist/renderer/assets/*                chat/agent UI bundles
Contents/Resources/app.asar.unpacked/dist/deps/tree-sitter-bash
```

The local-exec daemon exposes a single `"shell"` operation; the model-facing
tool names are not in the daemon. The coordinator references `GrokBotHarness`,
`AgentClient`, and `local-exec`.

**Conclusion (read in the bundle):** Grok Bot is an Electron client around a
separate local agent harness. The app owns the workspace, UI, permissions,
agent sessions, and streaming. There is no Python-style command regex in the
app.

---

## 3. The Grok harness (`@xai-official/grok` v1.0.62) — the real target

`/opt/homebrew/bin/grok` → `/opt/homebrew/lib/node_modules/@xai-official/grok/bin/grok`,
a 175 MB Mach-O. Its state lives in `~/.grok/`:

```
~/.grok/README.md        113 KB — the harness's own specification
~/.grok/config.toml      TOML configuration
~/.grok/bundled/agents/  explore.md, general-purpose.md, plan.md
~/.grok/bundled/roles/   implementer.toml, reviewer.toml, security-auditor.toml, …
~/.grok/bundled/skills/  SKILL.md bundles
~/.grok/memory-v2/       global + per-workspace memory, topics/ and observations/
~/.grok/sessions/<url-encoded-cwd>/<session-id>/
    system_prompt.txt     the exact prompt the model received
    tool_definitions.json the exact tool schemas the model received
    chat_history.jsonl    raw messages sent to the model
    updates.jsonl         the ACP session update stream
    events.jsonl, compaction/, signals.json, summary.json, terminal/, subagents/
```

**This directory is the finding.** A harness that persists the exact prompt
and the exact schemas is a harness whose contract is *data*, and that data is
readable.

### 3.1 The model-facing tool surface — 26 typed tools

From `~/.grok/sessions/.../tool_definitions.json` (26 entries, each
`{"type": "function", "function": {name, description, parameters}}`):

| Tool | Purpose | Required parameters |
| --- | --- | --- |
| `run_terminal_command` | run bash; background it, stream it, or wait | `command`, `description` |
| `read_file` | read a file | `target_file` |
| `search_replace` | precise edit | `file_path`, `old_string`, `new_string` |
| `list_dir` | list a directory | `target_directory` |
| `grep` | ripgrep search | `pattern` |
| `kill_command_or_subagent` | stop a background job | `task_id` |
| `todo_write` | **the model's own task list, shown live** | `todos` |
| `get_command_or_subagent_output` | read a background job's output | — |
| `spawn_subagent` | start a subagent | `prompt`, `description` |
| `scheduler_create` / `scheduler_delete` / `scheduler_list` | recurring prompts | — |
| `monitor` | watch a script's stdout as events | `command`, `description` |
| `search_tool` | **find MCP tools by keyword** | `query` |
| `use_tool` | **call an MCP tool found by `search_tool`** | — |
| `workflow` | run a Rhai script of subagents | `source` |
| `enter_plan_mode` / `exit_plan_mode` | plan, then ask the owner to approve | — |
| `ask_user_question` | **ask the owner one or more multiple-choice questions** | `questions` |
| `send_feedback` | save a local feedback draft | `title`, `details`, `type` |
| `web_fetch` | fetch a URL as markdown | `url` |
| `image_gen` / `image_edit` / `image_to_video` / `reference_to_video` | media | — |
| `write` | create or replace a file | `file_path`, `content` |

Three of these are the answer to "how does a harness handle a command nobody
wrote code for":

- **`search_tool` + `use_tool`** — the tool set is *discovered at runtime*, not
  hardcoded. A capability that exists at 09:00 is callable at 09:01.
- **`ask_user_question`** — the model asks, with options.
- **`todo_write`** — the model keeps a visible plan.

### 3.2 The system prompt is policy, not a router

`~/.grok/sessions/.../system_prompt.txt` (106 lines) is sectioned:

```
<dangerous_actions>  ask before hard-to-undo or shared-system actions
<work_policy>        complete every requirement; say when you could not
<memory>             where memory lives; what may be written; trust live evidence
<background_tasks>   the block_until_ms rules; use monitor
<scratch_files>      /tmp/ for scratch; delete what you make
<communication>      answer first; plain words; no contrast framing
<formatting>         GitHub-flavoured markdown
<browser_verification> verify a user-visible change in the browser
<memory-context>     the generated MEMORY.md index
```

Every line is about *how to behave*. Not one line says "when the user says
'open X', do Y". The prompt contains no command table, because the tool
schemas carry the vocabulary and the model does the routing.

### 3.3 Permission is a rule over the call, not over the sentence

`~/.grok/README.md` §Permission Rules:

| Prefix | Controls |
| --- | --- |
| `Bash(...)` | shell command execution |
| `Edit(...)` | file editing (path glob) |
| `Write(...)` | file writing (path glob) |
| `Read(...)` | file reading (path glob) |
| `Grep(...)` | search operations (path glob) |
| `WebFetch(...)` | URL fetching (glob or `domain:host`) |
| `MCPTool(...)` | MCP tool invocations |

Deny beats allow. `ToolPrefix(glob_pattern)`, with `*` and `**` supported, and
Claude Code's `Bash(cmd:*)` accepted as an equivalent.

**The permission is evaluated against the tool call and its arguments.** The
owner's sentence is never parsed for permission.

### 3.4 The sandbox is process-wide and irreversible

`~/.grok/README.md` §Sandbox: the sandbox is applied to the **entire grok
process at startup** using kernel primitives — Landlock on Linux ≥ 5.13,
Seatbelt on macOS — not per-command wrapping. "The sandbox is **irreversible**
once applied. This is a security feature — the model cannot convince the
agent to relax restrictions at runtime."

### 3.5 The harness measures itself

`~/.grok/sessions/.../signals.json` (one real session, 551 tool calls):

```json
{"turnCount":1,"toolCallCount":551,"toolFailureCount":1,"cancellationCount":1,
 "compactionCount":6,"totalTokensBeforeCompaction":1242326,"contextWindowUsage":68,
 "contextTokensUsed":175659,"contextWindowTokens":256000,
 "toolsUsed":["todo_write","read_file","run_terminal_command","grep","list_dir",
              "search_replace","get_command_or_subagent_output",
              "kill_command_or_subagent","monitor"],
 "avgTimeToFirstTokenMs":11350,"avgResponseTimeMs":16013,
 "itlP50Ms":9,"itlP99Ms":125,"peakRssBytes":148815872}
```

A harness that counts its own tool calls, its compaction events, its
time-to-first-token and its inter-token latency is an instrumented system, not
a script runner.

### 3.6 The harness speaks a protocol

`grok agent stdio` implements the **Agent Client Protocol** (ACP) — JSON-RPC
over stdin/stdout — with a Session Manager, a Tools Registry and MCP Servers
(`~/.grok/README.md` §ACP Protocol Reference). The app is the ACP client; the
harness is the ACP agent. Either side can be replaced without touching the
other.

---

## 4. OpenBot (v0.46.0) — the harness orchestra

`package.json` identifies `openbot` / `OpenBot` v0.46.0, repository
`github.com/nightly-labs/openbot`, license PolyForm Noncommercial 1.0.0. It is
an Electron + SolidJS desktop workspace with `node_modules/@anthropic-ai/claude-agent-sdk`
bundled.

Read from the source tree:

- `packages/contracts/src/agent-providers.ts` — **one table, one row per
  provider.** Its own header comment says the opposite shape existed first:
  *"A provider used to be written out by hand about twenty times — five copies
  of the same literal chain, four renderer lists, a preload destructure that
  threw on an id it did not know, an analytics regex — so adding one meant
  finding every copy, and missing one was silent."* The rows carry identity;
  the behaviour lives in `src/backend/provider-drivers.ts`. **The row carries
  the identity, the driver carries the behaviour.**
- The same file declares, per provider: `workspaceEnforcement`
  (`tool-sandbox` | `command-sandbox` | `confined-process`), `steer`
  (`native` | `deferred`), `concurrentTurns`, `reportsUsage`,
  `approvalsInFullAccess`. Those are the knobs of a harness, declared once.
- `src/backend/claude-client.ts:731` — `canUseTool` is the permission point.
  It returns `{behavior: "allow" | "deny"}` or asks the owner, and it is
  reached **from the tool call**, not from the prompt.
- `src/backend/claude-client.ts:834` — `CLAUDE_CODE_ENABLE_TODO_TOOLS: "1"`,
  with the reason in the comment: *"Without a terminal, the CLI gives the newer
  models no TodoWrite or task tools, so the agent has no plan for OpenBot to
  show as a task list."* OpenBot had to **switch the plan tools on** to get
  the behaviour.
- `packages/contracts/src/acp-agent-presets.ts` — known ACP agents, each
  `{command, args}`; the scanner *"never starts a file to learn what it is."*
- `packages/contracts/src/ipc-agent-runtime.ts` — the runtime's own contract:
  bounded text limits, `AgentRuntimePromptQuestion` with options, and
  `AgentRuntimeApproval` with `kind: "command" | "file-change" | "permissions"`.

**Conclusion (read in source):** OpenBot is a **harness orchestra**. Codex,
Claude, Grok, OpenCode, Gemini, Cursor, and Cline arrive through ACP; the app
never implements a per-command router. It supplies workspace isolation,
provider selection, permissions, and an event stream.

---

## 5. The architecture both share

```
owner turn
    │
    ▼
realtime/voice surface  ── conversation ──► spoken answer
    │
    │ work / action
    ▼
agent harness (model loop)
    │  system prompt = behaviour policy
    │  tool schemas = the vocabulary
    │  model decides: answer / ask / call tool
    ▼
local executor (shell, files, browser, computer use)
    │  every effect returns a receipt
    ▼
verified result ──► spoken or written answer
```

The app is **not** the decision-maker. The harness is. The app's Python (or
Electron main) is the body: tools, policy, persistence, streaming.

Three properties the references share, that a Python command table does not:

1. **The capability set is data.** Grok's 26 schemas and OpenBot's provider
   table are both *read*, not written per-command.
2. **The model asks.** `ask_user_question` and `AskUserQuestion` are tools.
3. **The model plans.** `todo_write` and `CLAUDE_CODE_ENABLE_TODO_TOOLS` are
   how both harnesses get a visible plan.

---

## 6. What Evie changed because of this

### 6.1 The capability graph is now the tool surface

Before this change the agent lane reached personal capabilities through one
generic tool that dispatched by **name string**:

```python
mac_call(name="life.send", arguments={"to": ..., "text": ...})
```

That shape had three faults, all visible in the working tree:

1. **No typed parameters.** The model only saw a one-line description of a
   capability, so it had to guess that `life.send` wanted `to` and `text`
   while `files.act` wanted `op` and `path`.
2. **A hand-maintained allowlist.** `mac_call` carried a hardcoded list of ten
   read-only capability names it would run without asking. A new capability
   had to be added to that Python list or it silently started asking for
   approval on a plain read.
3. **The discovery tool could not be discovered.** `capability.discover` — the
   one tool that answers "what can I do?" — was explicitly refused.

Now (`backend/app/agent/capability_bridge.py`), each of the 44 semantic
capabilities is **its own typed tool**, with schemas read from
`app/cognitive.capabilities.SEMANTIC_TOOLS` and execution dispatched to
`app.cognitive.executor.execute_semantic`. `mac_call` and `mac_capabilities`
are **removed** — with every capability typed, a name-string dispatcher is
strictly worse: no schema, and a second allowlist to maintain.

The model's surface went from 31 tools to **71**: 42 semantic capabilities
(one each, `agent.run` excluded because it owns the lane), 28 hands and
control tools, and `finish` / `ask_owner`.

### 6.2 The model keeps a plan and can offer a choice

- `todo_write` (Grok parity) records the run's visible plan and publishes it
  on the run's event stream. OpenBot had to switch the Claude SDK's plan tools
  on to get this; Evie simply has it.
- `ask_owner` now accepts `options` (Grok's `ask_user_question` parity) and
  the options travel with the `needs_input` result.

### 6.3 Permission is read off the capability's own row

`_classify` derives the fabric action class from each tool's own
`permission` (`messaging:send` → `send`, `fabric:shell` → `shell`) instead of
matching on tool names. A capability whose operation is a read — `owner.profile`
with `op=get`, `files.act` with `op=read`, `digital.act` with
`service=gmail, operation=search` — is **AUTO**, and the operation
vocabularies are imported from the owning lanes
(`app.ev.file_sandbox.READ_OPS`, `app.digital.tools._GMAIL_READ_OPS`) rather
than copied. A secret **path** is still DENYed, and the DENY check runs
before anything that would shortcut to AUTO.

### 6.4 The system prompt is sectioned policy, not a command script

`backend/app/agent/loop.py` `_SYSTEM` now mirrors the Grok sections:
`<dangerous_actions>`, `<work_policy>`, `<tools>`, `<plan>`, `<questions>`,
`<communication>`, `<formatting>`, `<environment>`. The old prompt carried two
Python-shaped crutches — "call `ask_owner` for the exact steps" for shorthand
goals, and "the Mac semantic bus is behind `mac_capabilities` + `mac_call`" —
both removed.

### 6.5 A long run stays inside the context window

`backend/app/agent/compaction.py`. Grok compacts at
`session.auto_compact_threshold_percent = 85`; Evie now compacts at
`agent_compact_threshold_percent` (default 85), counting the tool surface
because it is sent on every request. Compaction is **factual**: the system
prompt, the owner's goal and the model's own turns are kept verbatim, and a
compacted tool result becomes a receipt digest that still says whether the
call succeeded. Nothing is summarised by a model, because a generated summary
can state something the tool never returned.

---

## 7. Measured after the change

Live, real model (`grok-4.6`), real database, `EV_AGENT_PROVIDER=xai`:

| Turn | Route | Result |
| --- | --- | --- |
| "What can you do for mail and for my personal memory?" | agent → `capability.discover` ×5, `digital.discover`, `atlas.discover`, `memory.search` | 50.1s, 11 steps, answer built from receipts: *"Gmail is NATIVE (search/read/draft/send/reply/archive/label/trash…); Apple Mail live read failed with 403. Memory has no app atlas entry, but memory.search works and returned stored events."* |
| "Search my memory for anything about Ada Lovelace." | agent → `memory.search` | 9.9s, 1 step, grounded in the owner's real history: *"you've asked who she is, several times today — 13:25, 13:27, 13:31 IST; 04:18, 04:24, 04:30, 04:44 IST"* |
| "List the files in the workspace, read the first one, and summarise it." | agent → `todo_write` → `list_dir` → `run_shell` → `todo_write` | 14.4s, 4 steps, plan kept live, honest about the empty workspace |

Before the change the same "search my memory" turn could not be answered at
all from this lane: `memory.search` was not among its tools.

Gates: `make eval` → 25/25 gates, 161/161 checks, 5 skipped, exit 0.
`ruff check app/agent/` clean. `mypy app/agent/` clean (17 files).
207 agent/cognitive/turn-path tests green.

---

## 8. What is still not real

- **The harness still runs in-process.** Grok's harness is a separate
  program speaking ACP over stdio; OpenBot's is a separate CLI; Evie's loop
  runs inside the API worker. A hard crash in a native library still takes
  the turn with it. The next architectural step is a supervised child
  process with the ACP-shaped protocol Evie already knows how to speak.
- **No runtime tool discovery.** Grok's `search_tool`/`use_tool` let the model
  find a capability that was not in its schema. Evie's `capability.discover`
  does the same *job* (it returns the capability names for a domain) but the
  capabilities are still all in the prompt.
- **The provider bill is not paid.** The OpenRouter key on this machine
  returns `403 Key limit exceeded (total limit)`, and the DeepSeek direct key
  returns `402 Insufficient Balance`. The live proofs above therefore ran on
  the xAI key (`grok-4.6`), not on the configured DeepSeek main brain.
- **No pixel-grounded clicking.** Vision is read-only; clicking is
  Accessibility-grounded.
- **No per-run cost cap.** The harness records token usage in the result but
  does not stop a run that is spending.
- **The legacy conversational lane still has its own compact tool set**
  (`app/cognitive/speed.py`, 13 tools in compact mode). Both lanes now reach
  the same capabilities, but they are still two lanes with two budgets.

## 9. Licensing note

Grok Bot is proprietary; OpenBot is source-available under PolyForm
Noncommercial 1.0.0; `@xai-official/grok` is a published npm package. This
work studied behaviour and architecture for interoperability. Evie's
implementation is its own Python code and does not copy either codebase.
