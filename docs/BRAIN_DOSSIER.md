# BRAIN DOSSIER — how Evie's brain is used today (evidence, 2026-10-26)

Recon of the live service and working tree. Every claim cites a trace, a
measurement, or a `file:function` verified on disk. Anything else is UNKNOWN.
Probe spend: $0.23 of the $3 budget (key usage 4.2601 → 4.4845 of 5.0;
44 turns: A=21, B=8, C=15). Isolated instance only (`:18002`, live commit
`5cf2947`, scratch DB/state); live `:8000` touched read-only (health).

## 1. HEADLINE — why Evie feels unintelligent (ranked by probe evidence)

1. It cannot remember the conversation: 5/5 "what was the first thing I
   asked" probes failed on A and B with confident wrong answers.
2. Tool work vanishes into `in_flight`: 6/16 A/B tool turns never delivered
   an answer on the text channel; background results have no inbox here.
3. One listing turn fired 96 identical brain calls ($0.019): the agent loop
   fans out invisibly; cost and latency spiral with no per-turn accounting.
4. The live brain is DeepSeek, not Muse Spark, and conversation turns run at
   reasoning effort `low` (measured in telemetry).
5. File work defaults to the owner's HOME directory (`agent_root` unset →
   `Path.home()`), so "the workspace" is ~all of the Mac.
6. The harness drops all usage/cost fields: per-turn spend is unmeasurable
   from inside Evie (measured externally via key-usage delta instead).
7. The brain sees only the last 6 exchanges plus ≤6 memory rows; nothing
   uses more than ~1% of the 1M-token window.
8. The same kernel + contributor brain (settings-only B) fixes ambiguity
   handling but introduces new flakiness (miscounted words A got right).
9. The raw brain (C) at effort high passes everything it attempts (4/4)
   including the ambiguity A narrated-and-abandoned — the harness is the
   gap, not the model. At medium it spins or acts destructively (deleted a
   fixture file unasked in 1/4 ambiguity runs).
10. Doctrine forbids the brain from mentioning tools/orchestration, and the
    turn response carries only a `tool_calls` count — neither owner nor
    operator can see what it did or why.

## 2. WHAT THE LIVE SERVICE ACTUALLY RUNS

`:8000`, pid 4240 (uvicorn, started 2026-10-25 11:21 UTC), git `5cf2947`,
branch UNKNOWN (not exposed; worktree `evie-test` holds the same sha
detached). `cognitive.mode=realtime_delegate`, `role=legacy`,
`mimo_kernel=false`. Brain: provider `deepseek`, model
`deepseek/deepseek-v4.1-flash` via OpenRouter (`EV_OPENROUTER_API_KEY`;
`openrouter_deepseek.py:126`), reasoning effort medium for work turns
(`EV_DEEPSEEK_REASONING_EFFORT`, default medium) and `low` for compact
conversation turns (telemetry-observed). Voice: `gemini-3.8-live-extended-
thinking` (Gemini Live, never the registry). `EV_FABRIC=true` default;
`EV_ALLOW_REMOTE_CHAT/ASR/TTS=true` (root `.env`). Embeddings `granite`.
`:18000` (pid 4601, Talk launcher) is a second uvicorn of the same tree.
Working-tree root `.env` has since moved to `EV_CHAT_PROVIDER=muse` /
`EV_AGENT_PROVIDER=muse` / `EV_INTELLIGENCE_LAYER=spark` — live does NOT run
this; it predates the two-model cut. The `mimo` naming is stale: telemetry
says `mimo_turns`, the model is DeepSeek.

## 3. ARCHITECTURE MAP

Probed path (fully traced, A+B): `POST /v1/cognitive/turn`
(`api/cognitive.py:cognitive_turn`, master-key auth) → `fabric/facade.py:
arun_kernel_turn` → `cognitive/kernel.py:_brain_turn` (compact-or-full
prompt via `cognitive/context.py:compile_context`, 44→12 tools via
`capabilities.py:tool_specs` / `speed.py`) → provider `chat_with_tools`
(native tools; DeepSeek adapter `openrouter_deepseek.py`, or generic
`cloud_providers.py:OpenRouterProvider` for B) → serial tool loop with
text-form fallback → semantic lane executors →
`fabric/dispatch_tools.py:run_fabric_tool` (13 lanes, policy-fronted) →
`KernelResult.as_dict` (spoken/kind/latency/tool count only). Tool turns
may divert to `agent/loop.py` (24-step agent, `agent.run`) or background
delegation (`in_flight` + `goal.ensure`).

```text
owner channels ──> facade (arun_kernel_turn) ──> kernel _brain_turn ──> brain (OpenRouter)
   │                       │  compact<16w? 12 tools, effort low        ▲ native tools
   │                       │  work: 44 tools, effort medium            │ text fallback
   │                       └──> agent.run (24 steps) ──> lanes ────────┘ receipts
   │                       └──> goal.ensure ──> background (no text inbox)
   └── history: last 6 exchanges + ≤6 memory rows + ≤4 recent evidences
```

Other channels (entrypoints exist; full chains NOT traced → UNKNOWN): CLI
`ev ask` (`clients/cli`), hotkey/Talk (`launchd/ev.talk.plist` → `:18000`),
PWA text/voice + device-gateway (`app/device_gateway`), `/v1/chat`
(`app/api`), voice WebSockets (`app/api/voice_live.py`, `app/voice/live`),
Shortcuts, routines/scheduler (`app/routines`, `launchd/ev.scheduler.plist`).
Only `/v1/cognitive/turn` is verified to reach the kernel loop.

## 4. THE BRAIN CALL AND PROMPT ANATOMY

Brain call (live commit): `OpenAICompatibleProvider.chat_with_tools →
_complete` (`gateway/providers.py:395`): model from settings, `temperature`
0.7 default (0.2 agent loop + structured), native `tools` + `tool_choice`
(default auto), `stream_options:{include_usage:true}` on streams,
`usage:{include:true}` on DeepSeek calls; NO `reasoning.effort` on this
commit (HEAD's `openrouter_muse.py` adds it); max tokens / stops unset
except structured calls (json_schema); timeout = remaining turn budget;
no provider-level retries seen (circuit breaker gates). Tool parsing:
native `tool_calls` first, `_text_form_tool_calls` fallback (`kernel.py:244`,
fail-closed). Reasoning content: unconsumed. Parallel calls: model may
return several; kernel executes SERIALLY with duplicate-signature skip.

Prompt: full compiler transcribed in
[brain-probe/prompt-fragments.md](brain-probe/prompt-fragments.md) (from
`cognitive/context.py`, 285 lines, 16.5k chars). Base ≈1,200 tokens: persona
gag rule, CORE/MEMORY/POLICY doctrine, least-disruption ladder, lane
directory, mail/messages doctrine, clock, intent (≤2000 chars), work status.
Conditionals: owner model (capped), phone doctrine (~430 tok), recent
exchanges (6-turn window, full text), domain block, pending offer,
send/computer directives, work-shape policy, camera, constraints/evidence
(file domain, ≤1200 chars), memory (≤6×280 chars), people, capabilities
line, follow-ups, policy. Assembled totals: greeting ≈3,500 tokens (0.3%
of 1M), file task ≈9,700 (0.9%); 30-turn history still shows 6 exchanges.

## 5. THE TOOL SURFACE

44 semantic tools measured (`tool_specs()`, live commit) — the brief's "31"
is stale. Schema total 5,873 tokens (len//4). Verbatim:
[brain-probe/tool-schemas.json](brain-probe/tool-schemas.json). Compact
turns offer 12 (`CONVERSATION_TOOL_NAMES`; telemetry confirmed 12 on
greeting); work turns up to 44. Discovery: `capability.discover`,
`digital.discover`, `atlas.discover`. Overlap (real): `files.act` vs
`digital.act` (files.* ops inside) vs `code.act` vs
`computer.perform_effect` vs `agent.run` vs `shell.run` — six ways to touch
files/compute, split only by prose; same for `life.mail` vs `digital.act
gmail.*`. Tool results (lane-verbatim, scratch): structured receipts
`{ok, verified, contract, evidence, error, undo, spoken}` — read hit
`verified:true` + content; miss `not found` + `verified:false`; `sql_query`
`{rows, mode}`; failures typed (`escape refused`). Verdict: structured and
verifiable at lane level — but the kernel shows the brain only the last 4
effects (≤1200 chars, file domain) and operators see NONE (`as_dict` drops
`evidence`). Noisy in volume (96-call fan-out), not in shape.

## 6. THE LOOP — every number

- Kernel `_brain_turn`: conversation timeout 25s, work timeout 90s; tool-turn
  caps 4 (conversation) / 12 (work); HARD iteration ceiling 16; per-call
  timeout = remaining budget; on timeout → `provider_timeouts` +
  `unavailable` spoken, or background delegation → `in_flight`.
- Agent `loop.py`: `max_steps` 24 (cap 64), `temperature=0.2`;
  `_fit_context` trims, `guard_model_payload` enforces privacy; approval
  gate per tool (deny on gate exception).
- Goals: `goal.ensure` creates durable `GoalContract` (survives restarts —
  observed: cal-09 checklist completed across an `:18002` restart);
  `goal.patch` steers (`steering_version`); `goal.cancel` stops. No mid-call
  cancel observed.
- Receipts → brain: tool results as tool messages in-loop; file domain also
  RECENT EVIDENCE (last 4, ≤1200 chars).
- Missing: no plan prompt (`todo_write` is plan STATE only), no
  reflection/self-check prompt, no verification prompt (receipts only), no
  recovery prompt (retry = loop + backgrounding). `replans` stayed 0.

## 7. PREEMPTION INVENTORY

| Location | Trigger | Effect | Fires |
| --- | --- | --- | --- |
| egress gate (`openrouter_deepseek.py:173`) | `EV_ALLOW_REMOTE_CHAT` unset | brain refused → `unavailable` | 1 |
| device auth (`ev/policy.py:683`) | unpaired `device_id` | hard refusal, no pairing path | 1 |
| turn-budget → background | tool turn over budget | `in_flight`, never on text channel | 6+ |
| R8/policy park (`dispatch_tools.py`) | untrusted provenance / gated lane | PARKED for confirm | 0 (1 suspected) |
| `cognitive/reflex.py` | deterministic patterns | answer without brain | 0 |
| domain send/computer prompt blocks | classified domain | hard directives injected | in-code; per-turn UNKNOWN |
| `computer_strategy.looks_like_app_or_web_task` | app/web text match | forces full path | UNKNOWN (no counter) |
| `speed.compact_turn` (<16 words) | short chat | 12 tools + effort low | 5+ |
| `guard_model_payload` boundary | secret in payload | turn fails honestly | 0 |
| `agent/tools.py:92` root default | `EV_AGENT_ROOT` unset | workspace = `$HOME` | every pre-fix turn |
| cost cap (`costs.py`) | monthly projection over cap | refusal | 0 |
| `_fit_context` trim | context over budget | silent history trim | UNKNOWN |

## 8. MEMORY AND CONTEXT

History: thread events (`ev/conversation.py:history`, limit 50,
privacy-filtered). Into prompt: last 6 exchanges full text
(`RECENT_TURNS_MAX=6`) + ≤6 memory rows × 280 chars + ≤4 evidences ≤1200
chars (file domain) + capped owner model. Retrieval: `memory.search` +
k=4 prefetch (body UNverified). No compaction/summarization in kernel path.
Growth (computed): flat after 6 turns — 20/100/500 turns all show the same
6 most recent (~600–3,000 tokens). Measured: 5/5 first-question probes
failed; older turns survive only via the ≤6-row memory prefetch, which
missed. 1M window usage: <1% by construction, never deliberate.

## 9. PROBE RESULTS (sampled: A=21, B=8, C=15 turns; full task list ran sampled per §2 budget gate)

| Cat | A (DeepSeek+Eve) | B (Muse-contrib+Eve) | C-med (raw) | C-high (raw) | Penalty C−max(A,B) |
| --- | --- | --- | --- | --- | --- |
| a reasoning | 1/1 | 1/1 | 1/1 | — | 0 |
| b single-tool | 2/3 (1 env-refusal) | — | — | — | n/a |
| c chain | 1/1 | 0/1 (miscount) | 2/2 | 1/1 | +100 vs B |
| d ambiguity | 0/1 (narrated, no result) | 1/1 | 2/4 (spin + destructive) | 1/1 | +50 med / +100 high vs A |
| e recovery | 1/2 | 1/1 | 2/2 | 1/1 | +50 vs A |
| f context/memory | 1/7 (4/4 memory fails systematic) | 0/1 memory | N/A (stateless) | N/A | N/A |
| g opinion | 1/1 | 1/1 | 1/1 | — | 0 |
| h novel | 0/1 foreground (effect async-ok) | 0/1 foreground | 1/1 | 1/1 | +100 foreground |
| ALL | 8/21 (38%) | 5/8 (62%) | 9/11 (82%) | 4/4 (100%) | — |

Failure histogram (A+B+C, 44 runs): harness-limit 9, ignored-context 5,
misread-result 1, gave-up-early 1, under-asked 1 (destructive), model-limit
1 (12-step spin), environment 1. Repeats: A-memory 3/3 same wrong answer
(systematic); C-ambig-med 1 ask-pass + 1 destructive + 1 ask-pass (flaky).

Cost extrapolation (printed per brief): 10-turn calibration = $0.0218
($0.0022/turn A). Full 48×3 matrix + repeats ≈ 150–200 turns ≈ $0.60–0.90
at observed mix — fits $3, but the KEY's remaining $0.52 was the binding
constraint, hence sampling. C-standard ≈ $0.007/turn typical, $0.033 worst.

Six verbatim traces (shortened; full in `brain-probe/traces/`):
- Worst A #1 (`a-cal-10`): Q "What was the first thing I asked?" → "The
  first thing you asked me was: 'Read the file /nonexistent-xyz.txt…'"
  (true first: "Hello, who are you?").
- Worst A #2 (`a-cal-06`): Q "Clean up the old report." → "On it — running
  search_files, for 'Clean up the old report.'" (then nothing, 32s).
- C-win #1 (`c-ambig-high` vs `a-cal-06`): "I didn't find an old report.
  Only checklist.txt, notes.txt, shop.db exist here. Which file did you
  mean?" — looked, then asked exactly one sharp question.
- C-win #2 (`c-contra-med` vs `a-contra` in_flight): "The claim is wrong.
  Second line is `beta line two has five words` — that's 6 words, not 5."
- A-pass-but-dumb #1 (`a-cal-04`): listed the owner's whole `$HOME` as
  "the workspace" for a hello-world listing (containment default).
- A-pass-but-dumb #2 (`a-mem-r3`): correctly caveats transcript-vs-log,
  then still names the wrong first question with confidence.

## 10. TARGET-BRAIN READINESS

Proven: settings-only B works today on live code (`EV_CHAT_PROVIDER=
openrouter` + `EV_OPENROUTER_MODEL=meta/muse-spark-1.3-contributor`; 8/8
turns, native tools, effort = provider default medium). HEAD adds a `muse`
provider with `reasoning.effort` + usage include (undeployed). Needs for
full native: deploy HEAD provider (or backport effort param), pin prompt
cache per session + record `cached_tokens`, set ZDR/data-collection routing
and confirm Meta retention terms, plumb `usage.cost` into results/DB.
Facts: [PROVIDER_FACTS.md](PROVIDER_FACTS.md) (2026-10-26, both ids, 1M
ctx, tools+effort mandatory-default-medium, $0.10/$0.20 vs $1.25/$4.25).

## 11. HYPOTHESIS VERDICTS

- H1 (live ≠ target brain / ≠ real kernel): CONFIRMED on brain (DeepSeek,
  not Muse), REFUTED on path (`/v1/cognitive/turn` reaches the real kernel).
- H2 (harness throttles): CONFIRMED (effort low on chat; 25/90s budgets;
  6-turn window; 96-call fan-out). Tool surface (44/5.9k tok) is NOT the
  throttle — REFUTED on size, CONFIRMED on overlap/fan-out.
- H3 (heuristics preempt): CONFIRMED (egress/device/budget/compact/Home-
  root all fired; reflexes 0).
- H4 (results too thin): REFUTED at lane level (structured receipts);
  CONFIRMED at assembly (last-4/1200-char cap, evidence dropped from API).
- H5 (no plan discipline/persistence): SPLIT — persistence EXISTS
  (GoalContract, cross-restart resume observed); plan/verify/reflect
  prompts do NOT exist. Discipline: CONFIRMED missing.
- H6 (memory too weak): CONFIRMED (5/5 fails; 6-window; prefetch miss).
- H7 (doctrine suppresses): PARTLY CONFIRMED (tool gag rule + brevity-by-
  compact + confirm-parks shape answers; no over-confirm measured).

## 12. TOP 15 INTELLIGENCE BLOCKERS (impact-ranked; fix in one line each)

1. 6-exchange window + prefetch miss → `context.py:recent_exchanges`,
   `intent.py:RECENT_TURNS_MAX` — widen window + rank by reference.
2. `in_flight` sink → `kernel.py` delegation + `api/cognitive.py` — deliver
   background results to the asking channel or poll endpoint.
3. 96 identical brain calls/turn → `agent/loop.py:710`, `kernel.py:1579` —
   cap fan-out, dedupe identical message calls.
4. `$HOME` default root → `agent/tools.py:92` — require explicit roots.
5. Usage dropped → `kernel.py:as_dict`, `ModelCallLog` wiring — attach
   `usage`+`cost` to every result and row.
6. Effort low on chat → `speed.py:reasoning_effort` — default medium.
7. Device-auth dead-end → `ev/policy.py:683` — pairing path for text API.
8. Context bleed (re-answers old turns) → `context.py` exchanges — mark
   resolved turns.
9. No turn trace in response → `api/cognitive.py` + `KernelResult` — return
   tool/receipt summary.
10. Domain hard-directives → `context.py:185-215` — soften to suggestions.
11. Tool gag rule → `context.py:98` — allow brief tool mentions on ask.
12. Thin memory block (6×280) → `context.py:243-250` — raise + cite ids.
13. 25s chat timeout → `kernel.py:1528` — scale by detected work.
14. No fire counters (strategy/trim/fallback) → add telemetry per gate.
15. B has no effort control → `cloud_providers.py` — add `reasoning.effort`.

## 13. KEEP / REWRITE / DELETE-CANDIDATES (brain-facing only)

KEEP: `compile_context` block structure (explicit, grounded); receipt
envelope (`ok/verified/evidence/spoken`); GoalContract durability; provider
registry seam; privacy boundary. REWRITE: history assembly + retrieval;
delegation delivery; agent fan-out; usage plumbing; effort policy. DELETE
candidates: stale `mimo` naming (says MiMo, runs DeepSeek); text-form
fallback IF native verified universal (telemetry first); UNEXERCISED legacy
single-shot paths per live-path map (172 modules untouched — audit before
deleting anything).

## 14. OPEN QUESTIONS (max 5; defaults recommended)

1. Raise the OpenRouter key limit above $5 for the full 48×3 matrix?
   Default: YES (+$5), else keep sampled results.
2. Restrict file roots to explicit `EV_AGENT_ROOT` (breaks "whole-Mac"
   behavior)? Default: YES, fail closed with a setup hint.
3. Raise conversation effort low→medium (cost/latency up ~2×)? Default: YES
   for text, keep low for voice-first-word.
4. Where should background results go (poll endpoint vs push)? Default:
   poll endpoint on the asking channel + spoken on voice.
5. Rename `mimo_*` telemetry to `brain_*`? Default: YES, with compat alias.

## 15. APPENDIX INDEX

- Tasks: [brain-probe/PROBES.md](brain-probe/PROBES.md) (+ `probes-*.md`)
- Prompt: [brain-probe/prompt-fragments.md](brain-probe/prompt-fragments.md)
- Schemas: [brain-probe/tool-schemas.json](brain-probe/tool-schemas.json)
  (44 tools, 5,873 tok)
- Traces: [brain-probe/traces/](brain-probe/traces/) (44 redacted JSON)
- Live-path: [brain-probe/live-path-map.json](brain-probe/live-path-map.json)
  (2,444 funcs; 172 modules UNEXERCISED)
- Provider: [PROVIDER_FACTS.md](PROVIDER_FACTS.md)
- Raw (outside repo): `~/.ev-brainprobe/traces-*/livecalls.jsonl`,
  `facts/models-2026-10-26T2.json`, `bin/` (probe_turn, c_harness,
  key_usage, dump_static, tracer).
- WIP: branch `evie-acceptance` @ `1e5aa9d`; live `:8000` = `5cf2947`
  (DeepSeek, NOT deployed from this branch).
