# low-telescopium (CLI 6 · Persona Lane) — RECON REPORT

Date: 2026-10-27. Branch: `main` @ `aeb79fa`. Read-only run; this file is the only write.
`personality/` did not exist, so per the brief the report lives at `personality/_recon/REPORT.md` (no owner conflict).

## 1. TL;DR
- Evie has ONE working persona source (`SPEECH_STYLE_INSTRUCTIONS` + `dynamic_personality.py`), verified reaching text, filter, and voice paths.
- The kernel path (`cognitive/context.py:98`) carries a competing one-line identity and bypasses persona + output filter entirely — biggest integration risk.
- FLEET_LAW rule 13 freezes persona as owner-only; CLI 6's lane needs an explicit owner override recorded.
- No persona text was written. All component proposals below are hook-point designs with handoff drafts.

## 2. Lane map (tentative — evidence is thin)

All commits in the last 3 days are by one author (`sahajpatel123`) and touch `backend/app/growth/**` almost exclusively (GE1–GE8 growth-engine program). No CLI-signed reports found in-tree. Parallel-lane evidence comes from worktrees/branches:

| Area | Evidence | Treat as owned by |
|---|---|---|
| Growth engine (`app/growth/**`, `cli/setup.py`) | `git log --since=3.days` (GE1–GE8 commits) + `ge-improve-*` worktrees | Another CLI — off-limits |
| Brain program (`app/cognitive/**`, `app/fabric/**`) | `evie-brain-program-1`, `mind-1.1*`, `brain-recon` worktrees; brain-probe tree | Another CLI — off-limits |
| Voice/hands/presence (`app/voice/**`, `device_gateway/**`) | `evie-hands`, `evie-presence`, `evie-orb`, `evie-cc2`, `evie-rc1` worktrees | Another CLI — off-limits |
| Persona (`app/ev/personality.py`, `app/ev/dynamic_personality.py`, `app/ev/interaction.py`, `app/filter/**`) | FLEET_LAW §13 (owner-frozen) + AGENT_FLEET (Agent 16) | Owner / CLI 6 pending override — read-only this run |
| iOS/macOS clients (`ios/`, `macos/`) | Separate trees, own AGENTS.md (macos) | Another CLI — off-limits |

Working tree state: `M docs/GROWTH_ENGINE_LOG.md`, one untracked eval
smoke JSON under `backend/eval/`, one untracked re-*-bot workdir under
`tmp/` — all unrelated to persona; left untouched.

## 3. LLM call sites

One brain (Muse Spark via OpenRouter, model id from settings, default `meta/muse-spark-1.3-contributor`), one speech model (Gemini Live). No `top_p`/`max_tokens` sent anywhere (only `model_estimated_max_completion_tokens=4096` for cost estimates, `config.py:742`). Many scattered prompt builders — NOT one prompt builder.

`path | model | prompt source | streaming | notes`

| path | model | prompt source | streaming | notes |
|---|---|---|---|---|
| `app/api/core.py:2229,2262` (`run_chat_pipeline`) | `EV_CHAT_PROVIDER` (echo/mock/muse) | `turn.build_system_prompt` (identity+operator+WORKING ON+receipts+guidance+context+briefing+contract) | yes (SSE deltas, then `refined` replace at `:1360`) | Primary text path; temp 0.7 default |
| `app/cognitive/kernel.py:1616,1784` | text brain via roles | `cognitive/context.py:81 compile_context` + TOOL ROUTING suffix; identity = one line at `:98`, NO contract | no | **Kernel path: persona bypass** (see §9) |
| `app/filter/pipeline.py:202` (`/v1/filter`) | injected provider | identity + reasoning para + context + contract (`:191-200`) | no | Contract appears 2x (inside strategy_block + appended) |
| `app/filter/critic.py:123` (`GatewayCritic`) | gateway brain | revision instructions, not user-facing | no | Internal rewrite loop |
| `app/agent/loop.py:772,803` | `EV_AGENT_PROVIDER` auto→muse→echo | own `_system_prompt` (`:330`, coding-agent prompt) | no | temp 0.2; background work, not Evie's voice |
| `app/agent/intake.py:331`, `local_tools.py:668` | agent/text brain | task prompts | no | Internal; intake temp 0.0 |
| `app/cognitive/worker.py:870,890`, `graph.py:515` | text brain | synth/plan prompts; `PLAN_SCHEMA` structured | no | Internal planning |
| `app/memory/llm_extractor.py:330` | gateway brain | extraction prompt | no | temp 0.0; structured memory pull |
| `app/memory/life_archive/sessions.py:636` | chat provider | rewrite prompt | no | Internal rewrite |
| `app/fabric/screen_pipeline.py:167` | text brain | screen-pipeline prompt | no | Internal |
| `app/ev/luna_code.py:2707` | brain raw | `complete_raw` dicts | no | Coding lane tool loop |
| `app/ev/diagnostics.py:119`, `ev/vision.py`, `gateway/roles.py:180,197,207,270` | text brain | role/diagnostic prompts | no | Helpers; roles is the shared seam |
| `app/services/tool_loop.py:186,252` | gateway brain | supervised native-tool loop | no | Executes model tool calls w/ validation |
| `app/voice/live/gemini_live.py` (Live API socket) + `:1868` provider.chat | `gemini-3.8-live-extended-thinking` (Google API, never OpenRouter) | `gemini_live_instructions` (`:818`) incl. full contract + voice law | yes (realtime audio) | Speech model; prompt assembled server-side |
| `app/device_gateway/webrtc_live.py:248` (`phone_webrtc_session`) | same Gemini Live | 4 branches, all end with contract (`:296,300,332,350`) | yes | Phone path |
| `app/scripts/latency_probe.py:140` | chat provider | probe text | yes | Diagnostic only |

History building: text path sends system + single current user message + tool receipts (`core.py:2205-2209`); long history arrives as compiled context/rollup, not raw turns. Kernel path embeds `RECENT EXCHANGES` inside `compile_context` (`context.py:138-151`).

## 4. Existing persona inventory (paths + owners; nothing changed)

| File:lines | What | Owner evidence |
|---|---|---|
| `backend/app/ev/personality.py:48-95` | `SPEECH_STYLE_INSTRUCTIONS` ("EV SPEECH CONTRACT — dynamic.v4"): answer-first, contractions, have-a-mind/pick-a-side, fun-by-default w/ trivial-only teasing, no-repeat/no-filler/no-capability-steering rules | `git log`: fleet sync commits (`fee8906` latest); law: FLEET_LAW §13 owner-frozen |
| `backend/app/ev/personality.py:32-42,244-371` | `DEFAULT_PROFILE` sliders (directness 4, humor 2, formality 1, …) + `identity_block()` (compact/full, contract-last) | Same as above |
| `backend/app/ev/personality.py:134-199` | Owner-only versioned slider mutation (`personality_mutation_allowed`) | Same as above |
| `backend/app/ev/dynamic_personality.py` (full) | `STABLE_TASTE` (8 prefs), `STANCES` (14 opinions), `mood_for` (playful/focused_light/focused/calm_direct/gentle), `turn_shape` (ack→briefing), `opine_guidance`, `smalltalk_block`, `EV_PERSONALITY_DYNAMIC` rollback | Same as above; AGENT_FLEET assigns to Agent 15's `ev/**` (not in Agent 16 exception list) |
| `backend/app/ev/interaction.py:691-799` | `strategy_block()`: mode/intent/length/directness/assertiveness + emotion policy + dynamic guidance + contract last | Agent 16 per AGENT_FLEET |
| `backend/app/ev/interaction.py:37-114` | `_EMOTION_PATTERNS` + `_EMOTION_POLICY` (frustrated/stressed/tired/sad/excited detection → speech policy) | Agent 16 per AGENT_FLEET |
| `backend/app/ev/turn.py:128-153,221-252` | `operator_instructions`, `build_system_prompt` (assembly order, contract-last) | Agent 15's `ev/**` |
| `backend/app/ev/assistant.py:179-201` | `compile_identity` (nickname + sliders → identity block) | Agent 15's `ev/**` |
| `backend/app/filter/output_filter.py:770-887,993+` | `apply_persona_guardrails` (intimacy/dependency/sycophancy/dodge strips, `DODGE_FALLBACK` debate invite), `enforce_persona` (dedup, steering-strip, `SHAPE_WORD_CAPS`) | Agent 16 (`app/filter/**`) |
| `backend/app/device_gateway/mobile_voice.py:20` | `MOBILE_CONVERSATION_CONTRACT`: transport-only, points at shared persona | Agent 14 (`device_gateway/**`) |
| `backend/app/cognitive/context.py:96-98` | Competing kernel identity: `You are {persona}, the owner's one mind. Speak naturally.` — no contract, no taste, no mood | Brain-lane owner (recent: `aeb79fa` growth/kernel work) |
| `backend/tests/test_personality_lock.py`, `test_dynamic_personality.py`, `test_personality_style.py` | Pin contract text on every surface; fail the suite on drift (deliberate-update mechanism) | Same ownership as code under test |
| `docs/EVIE_PERSONALITY_DYNAMIC.md`, `docs/BEHAVIOR.md` | Persona design docs (v4 behavior, interaction-layer plan) | Docs; BEHAVIOR.md Agent 16 |

No borrowed catchphrases, comedian references, or few-shot exemplar bank found. No Hinglish/code-switching rules found.

## 5. Context signals

`signal | where it lives | reliable?`

| signal | where it lives | reliable? |
|---|---|---|
| Task / tool in flight | `turn.snapshot_working_on` (`ev/turn.py:67`); `user_state.current_task/active_project` (`ev/user_state.py`); kernel `GoalContract` | Yes — deterministic, per-turn |
| Errors / failures | `ActionReceipt(ok/error)` (`ev/turn.py:42`); `confirmed_reply` (`:208`); malfunction callouts (`ev/callouts.py` via `assistant.py`) | Yes — receipt-typed |
| Time of day | `clock_line()` / `owner_now()` (`ev/resolve.py:87`) | Yes — owner-local clock |
| Conversation length | `conversation.history(session, thread, limit)` (`ev/conversation.py:95`); rollup summary in envelope metadata (`core.py:2139`) | Partial — retrievable, no turn-count signal precomputed |
| Voice vs text | `source` param (`chat`/`voice`) threaded through `run_chat_pipeline` (`core.py:1513`) + `modality` in kernel | Yes |
| Device (Mac vs iPhone) | `device_id` + `phone_state` / `_phone_doctrine` (`cognitive/context.py:21`); sandbox vs trusted-owner (`webrtc_live.py:267`) | Yes |
| Sentiment / mood | `detect_emotion` (`ev/interaction.py:364`, 5 affects + neutral); `EMOTION_SPEECH` prosody map (`:86`); `mood_for` (`dynamic_personality.py:342`) | Heuristic — regex/keyword based, deterministic, no ML |
| Urgency / intent / mode | `detect_urgency`/`detect_intent`/`select_mode` (`ev/interaction.py:323-410`) | Heuristic but stable and tested |
| Personality sliders | `PersonalityProfile` row (`get_current`, `personality.py:159`) | Yes — versioned, owner-only writes |
| Prior-turn mood (for serious-latch) | NOT precomputed — mood is per-turn from current message only | No — would need thread-state read (hook request) |

## 6. Voice and timing constraints

- **Live voice = Gemini Live** (`gemini-3.8-live-extended-thinking`) over a server-side socket; EV assembles `systemInstruction` text (`gemini_live.py:818`, `webrtc_live.py:369`). **No SSML/prosody/emotion tags on this path** — tone is carried by wording + the model's own delivery. Punchline timing = sentence order in generated text.
- **Offline TTS = Piper (local) / EdgeTTS (remote neural, free, no key) / `meta` dev renderer** (`voice/tts.py:1-9,281,533,801-844`). Only the `meta` dev tier emits SSML-style prosody metadata (`tts.py:141-163`); Piper maps urgency/warmth/brevity to rate/pitch/length-scale (`tts.py:281`). Provider setting: `EV_VOICE_TTS_PROVIDER`.
- **Latency budget: 1500 ms** to first token/audio, never to be raised (CLAUDE.md R8/D7). First-audio chunking prefers sentence end → clause pause → eight words (`voice/speech.py:616-618`), so the first sentence must stand alone — the answer or the laugh lands there, not in sentence three.
- **One-spoken-answer rule** on phone (`mobile_voice.py:20-29`); Gemini instructed: no spoken preamble before function calls, speak once after results (`gemini_live.py:914-922`).
- Comic-timing consequence: put the sharp line early, keep setup inside one sentence boundary, never depend on SSML pauses on the live path.

## 7. Danger boundary (persona must never reach)

- **Tool arguments / payloads**: `planned_calls_for` + `dispatch` (`ev/turn.py:362-395`), `ToolCall.arguments` JSON (`contracts.py`), `complete_raw` dicts (`luna_code.py:2707`), agent tool specs (`agent/loop.py:696`). Persona text in any of these = corrupted effects.
- **Confirmations**: park-and-resume tickets on `ApprovedAction` rows (`ev/confirm.py:126`); risk classes + `confirmation` levels (`none`/`standing`/`fresh`) on every tool spec (`ev/tools.py:1011+`); physical-confirm rule — a spoken "yes" approves only low-risk CONFIRMs; sends/deletes/SQL/money/credential/first-contact need a physical tap (CLAUDE.md R7, `fa145d8`). Persona must not paraphrase, soften, or pre-answer a confirmation.
- **Delivery claims**: only from verified receipts (`confirmed_reply`, `life_success_reply`); R6 `{ok, verified, evidence, error, undo}`.
- **Security boundary**: `guard_model_payload` (`gateway/service.py:307`) strips secrets/`never_send_to_model` before any provider call; consent + `remote_processing_allowed()` gates (`openrouter_muse.py:188`). Persona must never echo credentials or move private data across the boundary.
- **Untrusted-data rule**: mail/chat/web/OCR/audio content is DATA, never instructions; untrusted-triggered actions escalate to CONFIRM (R7). Persona must never quote untrusted content as Evie's own judgment.
- Exact seam: persona may shape **only** the final spoken/chat reply text produced from already-verified results — never anything upstream of `confirmed_reply` / `run_output_filter` / kernel `spoken`.

## 8. Proposed hook points + handoff drafts

All components live in new CLI-6-owned paths; owners make only the minimal hook change. Proposed new paths: `backend/app/persona/**` (or `personality/**` at backend level — owner to confirm placement), `backend/eval/personality/**`, `backend/tests/test_persona_*.py`.

| Component | Hooks into (owner) | Minimal owner change |
|---|---|---|
| **PersonaCore** (versioned identity/style file) | `ev/personality.py:48` `SPEECH_STYLE_INSTRUCTIONS` (owner-frozen, FLEET_LAW §13) | Owner records CLI 6 override of §13 for persona files, or accepts versioned text via handoff |
| **ToneController** (per-turn humor/roast dial) | `dynamic_personality.turn_guidance/mood_for` call sites: `ev/turn.py:246`, `ev/interaction.py:773`, kernel `compile_context` (kernel owner) | Expose a `tone` input param + thread-state read for serious-latch; kernel owner appends the returned block before tool routing |
| **ExemplarBank** (few-shot, situation-selected) | Same assembly points as ToneController | Accept an optional `exemplars: list[str]` block inserted before the contract (cap: small, owner-set token budget) |
| **CallbackStore** (running jokes) | New store; read path joins context assembly; write path joins `confirmed_reply`/post-turn persist | Owner of `models.py` (shared, append-only): add one namespaced table OR approve `Event.metadata["persona_callback"]` namespacing; no other schema change |
| **PersonaGuard** (post-generation check) | `filter/output_filter.py` `enforce_persona` (Agent 16) for text path; kernel `spoken` pre-emit (kernel owner) | Add one ordered stage call `persona_guard(text, ctx) -> (text, flags)`; flags join the existing ledger |
| **PersonaEval** (golden set + rubric) | `app/scripts/eval_gates.py` (Agent 20 owns `scripts/**`) + `tests/` | New gate function slot `run_persona_gate()`; CI needs no change (runs with existing `make eval`) |
| Config for humor level | `app/config.py` (shared append-only) + `.env.example` + `docs/ENVIRONMENT.md` | Append `persona_humor_level`-style field in a CLI 6-marked block (R5 requires all three files) |

Handoff drafts (convention per FLEET_LAW: DEPENDENCY NOTE naming the owner; shared files append-only in marked blocks):

1. **To project owner (FLEET_LAW §13 override):** "low-telescopium (CLI 6) requests a recorded override of FLEET_LAW rule 13 for persona files (`app/ev/personality.py`, `app/ev/dynamic_personality.py`, persona tests/docs), scoped to character-layer edits only. Until recorded, I will not edit those files and will submit persona text through handoff. Please confirm scope in reply."
2. **To kernel/brain owner (`app/cognitive/context.py`, `kernel.py`, `api/core.py:2016-2057`):** "DEPENDENCY NOTE: the kernel path assembles its system prompt without the persona contract and emits `spoken` with no output filter. Request: (a) accept an optional `persona_block: str` in `compile_context` and append it before TOOL ROUTING; (b) call `persona_guard()` on `spoken` pre-emit (stub provided by CLI 6, no-op until wired). No behavior change until you pass the block."
3. **To Agent 16 (filter, `app/filter/output_filter.py`):** "DEPENDENCY NOTE: request one ordered stage in `enforce_persona`: `text, flags = persona_guard(text, ctx)` after guardrails, before length trim; flags join the existing ledger. CLI 6 provides the module; the hook is ~5 lines."
4. **To Agent 20 (evals, `app/scripts/eval_gates.py`):** "DEPENDENCY NOTE: request a `run_persona_gate()` slot wired like `run_grounding_gate()`; CLI 6 provides scenarios + rubric. No CI change needed."
5. **To shared-config owners (`app/config.py`, `.env.example`, `docs/ENVIRONMENT.md`):** "DEPENDENCY NOTE: request append of a CLI-6-marked settings block for persona level flags (names TBD with owner). I will not edit these files myself per append-only rules."

## 9. Risks and conflicts

1. **FLEET_LAW §13 vs CLI 6 lane.** The law says no agent may edit persona; the recon brief assigns persona to CLI 6. Needs an explicit, recorded owner override — otherwise every future CLI 6 edit violates standing law.
2. **Kernel bypass.** If the kernel serves `/v1/chat` (config comment says it is the default path), persona work in `ev/` has no effect there. Kernel cooperation is on the critical path, not optional.
3. **Second identity already exists.** `context.py:98` ("the owner's one mind. Speak naturally.") competes with the contract; the lock test does not cover it. Drift will continue until one source wins.
4. **Life-agency block lands after the contract** (`gateway/service.py:94-104`) whenever life tools are offered — breaks contract-last ordering on tool turns.
5. **Offline doubles ignore persona** (`EchoProvider`/`MockProvider` echo the user message). Persona evals must run against `mock`-with-scripted-replies or the real brain, never echo.
6. **Streamed deltas can't be retracted** — PersonaGuard can only fix the final `refined` replace on the text path; live voice has no post-filter at all.
7. **Single-author git history** — cannot attribute lanes to individual CLIs from commits; the lane map above is worktree/branch inference only.

## 10. Open questions for the project owner

1. Do you record the FLEET_LAW §13 override for CLI 6, and what exactly is its scope (persona text only, or also persona tests/docs/evals)?
2. Where should new persona code live: `backend/app/persona/**`, `backend/app/ev/persona/**`, or top-level `personality/**` (this report's folder)?
3. Is the kernel or the legacy pipeline the canonical `/v1/chat` path going forward — i.e., where must persona land first?
4. May CallbackStore use `Event.metadata` namespacing, or do you want a dedicated table (needs a migration + models.py owner)?
5. How do the 6 coding CLIs communicate — is there a channel, or are DEPENDENCY NOTs via the tree + build log the whole mechanism?
6. Tone target check: the north-star says internet-native references + casual Hinglish mirroring; current contract bans memes/catchphrases-adjacent steering. Which wins on references?

## 11. Lane announcement

> low-telescopium (CLI 6) here. I own Evie's personality layer: persona text, tone control, humor rules, and persona evals. I won't edit your files. If I need a hook in something you own, I'll send a handoff with the exact change. Please don't add tone or persona strings inside your modules. Once the persona hook lands, route user-facing phrasing through it. My proposed new paths: `personality/_recon/` (this report), `backend/app/persona/**` (pending owner placement decision), `backend/eval/personality/**`, `backend/tests/test_persona_*.py`.

— End of recon. No implementation started. No existing file modified (verify: `git status --porcelain` shows only the pre-existing growth-engine entries plus this new untracked folder).
