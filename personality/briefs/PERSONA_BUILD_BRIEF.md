# low-telescopium — Persona Build Brief (Phase 1, persona.v2)

Self-contained: there is no other brief or addendum to look for.
Owner approval: build Phase 1 fresh, implementing persona.v2 directly. Your paths: backend/app/persona/**, backend/tests/test_persona_*.py, backend/eval/personality/**, personality/**. Everything else is off-limits, including the fleet-owned test_personality_* files.
Source of truth: personality/briefs/evie-character-v2.md. Copy it verbatim to backend/app/persona/spec/CHARACTER.md. All persona wording compiles from it. If the spec seems wrong, propose the change in your report; never invent wording.

## 0. Non-negotiable rules
1. Edit nothing outside your paths. You may import and read existing functions (ev/personality.py, ev/dynamic_personality.py, ev/interaction.py, ev/turn.py, cognitive/context.py, cognitive/kernel.py, filter/**, config.py, models.py, voice/**, device_gateway/**, the agent lane); never modify them.
2. No LLM calls on the hot path. app/persona/ is deterministic pure Python with no network access, under 5 ms per call. The 1500 ms first-audio budget is law.
3. Persona block budget: text ≤ 650 tokens (core + NOW line + turn tone + up to 2 exemplars), voice ≤ 380. When over, drop in this order: exemplars, then [text]-marked lines, then NOW-line detail. Never drop OPERATIONAL LINES.
4. Persona never touches upstream data: tool arguments, confirmation text, receipts, delivery claims, or anything before confirmed_reply / run_output_filter / kernel spoken.
5. Reuse the existing detectors: detect_emotion, detect_urgency, detect_intent, select_mode, mood_for, ActionReceipt, owner_now, and the source/channel param. Don't re-implement them.
6. Flag: PERSONA_ENABLED = False in app/persona/settings.py. The formal config field arrives via handoff (R5).
7. No real-person names, comedian references, or quotes anywhere in the package, spec copy, or exemplars. A test enforces this.

## 1. Package layout (backend/app/persona/)
- __init__.py: exports build_persona_block, persona_guard, PERSONA_VERSION = "persona.v2"
- spec/CHARACTER.md: verbatim copy of the spec
- settings.py: PERSONA_ENABLED=False, profanity=False, night_start="00:30", nudge_gap_min=90, max_nudges=2, wake_time=None, brief_mode="first_message_after_06:00", night_scope="continue_current_task", joke_rate_bounds=(0.15, 0.7)
- core.py: extracts the PERSONA CORE BLOCK from spec §2; text and voice variants
- context.py: PersonaContext (section 2)
- audience.py: private | on_behalf | company; resolve_audience(ctx), default private
- awareness.py: build_now_line(signals) per spec §5
- tone.py: ToneDecision + decide_tone(ctx, weights) per spec §4
- techniques.py: the 7 techniques from spec §2, each with id, one-line definition, min_roast, allowed audiences
- reactions.py: classify_reaction(owner_msg, prev_meta) -> positive|neutral|negative; roast_me_anyway(msg); banter_invited(msg); hinglish(msg)
- learning.py: per-owner Beta weights per technique and topic, plus the adaptive joke rate; Thompson sampling with an RNG seeded from (thread_id, turn_index)
- callbacks.py: CallbackStore protocol + InMemory + EventMetadata adapter (unwired); landed bits + the no-repeat line store
- opinions.py: OpinionLedger protocol + InMemory + EventMetadata adapter (unwired)
- exemplars.py + exemplars.yaml: seed from spec §9; tag-based select(), max 2, token-budgeted, deterministic; keep {} placeholders
- nightshift.py: section 5
- guard.py: persona_guard(text, ctx) -> (text, flags)
- compile.py: build_persona_block(ctx, store=None) -> PersonaBlock — THE single entry point
- tokens.py: token estimator; reuse the repo's existing one if present

## 2. The single entry point
PersonaContext (frozen dataclass, all optional except channel): channel ("text"|"voice"|"phone"), audience (default "private"), emotion, urgency, intent, mode, receipts (tuple, read-only), confirmation_pending, sensitive_topic, owner_msg, local_time, session_started, work_state (task, last test/build result, files in flight), first_commitment_tomorrow, humor_slider (read-only), serious_latch_turns_left, recent_joke_flags (last 3), last_joke_meta (technique, topic), night_state, thread_id, turn_index.
PersonaBlock (frozen): text, version, tone (ToneDecision: roast, humour_allowed, technique, profanity, audience, reasons[]), token_estimate.
build_persona_block returns: core (by channel) + NOW line + "TURN TONE: ROAST=n; AUDIENCE=…; PROFANITY=on|off; TECHNIQUE=…|none" + up to 2 exemplars. Unknown signals always degrade toward calm. Every surface — legacy text, kernel, filter, Gemini Live, phone WebRTC — must eventually call this one function.

## 3. ToneController (tone.py)
Implement spec §4 as an ordered rule list. Every rule appends a readable reason to reasons[]. Must hold: rule 1 has no override; "roast me anyway" lifts the ceiling to 2 for one turn only and clears the serious-latch; frustration humour targets the object, never the owner; company mode caps roast at 1 and turns profanity off; on_behalf carries no Evie voice; voice caps roast at 2.
Also export update_latch(prev, decision) -> int and joke_flag(reply, decision) -> bool for the thread-state owner.

## 4. Learning (learning.py, reactions.py)
- Reaction classification is deterministic and high-precision; anything unclear counts as neutral.
- Beta(α, β) weights per technique and per topic. The joke rate adapts within joke_rate_bounds.
- hint_for(decision, weights, rng) returns a technique id, or None when ROAST=0 or the joke-rate draw says skip.
- Pure functions; persistence only through the CallbackStore interface.

## 5. Night Shift (nightshift.py)
Conversation and plan only; execution belongs to the agent lane (handoff).
- fatigue_signals(recent_owner_msgs, receipts): rising typo rate, the same failure 3 or more times, "tired", "one more", "last thing".
- should_nudge(ctx, night_state): after night_start, with a session of 2h+ or fatigue; at most max_nudges a night; at least nudge_gap_min between nudges; the second nudge needs new signals; a decline is recorded and respected.
- NightShiftPlan (serializable dataclass): goal, task_ref, workspace ("evie/night-YYYYMMDD"), allowed (read, edit in workspace, run tests, local build, research, write docs, stage PR without merge), forbidden — a constant config cannot change (push to main, merge, deploy, send, delete outside workspace, spend money, use credentials, first contact) — stop_conditions (goal met, time budget, wake minus 30 minutes, 3 consecutive failures on the same step), checkpoint_every_min, confirmation_queue, brief_at.
- Scope default: continue the current task only.
- plan_from_context(ctx) returns None when the task is unclear (Evie then asks one question). validate(plan) rejects any plan naming a forbidden capability or exceeding its scope.
- render_offer(plan) and render_plan_summary(plan) return structured slots, not wording; the model writes the words through the persona block.
- MorningBrief: done[] (each item requires a receipt reference), tests, waiting_for_you[], next_steps[], night_line_slot.

## 6. Guard (guard.py)
- Only two mutations: strip the banned openers/closers from spec §10, and cap emoji (0 in voice, 1 in text).
- Flag only: humour markers in a ROAST=0 turn; extra text on a confirmation turn; a voice first sentence over ~25 words; a line repeating the no-repeat store; profanity or Evie-isms in on_behalf text (strip the profanity, flag the rest).
- Idempotent. Never alters confirmation or receipt-derived text.

## 7. Tests (backend/tests/test_persona_*.py)
- Tone: every spec §4 rule, the one-turn roast-me-anyway lift, audience caps, latch decay, unknown signals defaulting to calm.
- Budget: text ≤ 650 and voice ≤ 380 tokens across all selection paths; OPERATIONAL LINES always present.
- Awareness: missing signals omitted, no invented values, ~40 tokens max.
- Learning simulation: a scripted owner who rewards understatement and punishes escalation moves the weights correctly within 30 turns; the joke rate falls under repeated negatives.
- Reactions: a labelled fixture of 60 messages; precision ≥ 0.9 on positive and on negative.
- Night Shift: nudge caps, gap and decline; forbidden or out-of-scope plans fail validation; stop conditions; unclear task returns None; the brief accepts only items with receipt references.
- Guard: repeat detection, on_behalf leak handling, idempotence, confirmation text untouched.
- Boundary and purity: no network or LLM imports in app/persona/ (assert on the import graph); under 5 ms per call; deterministic output for the same context.
- Name-ban: grep the package and spec copy against a denylist kept only inside the test file, plus the word "comedian".

## 8. Eval harness (backend/eval/personality/)
- scenarios.yaml: about 50 scenarios, each with context signals and the expected tone ceiling. Families: casual banter, coding help, opinion, candor about products and companies, overruled, wrong-earlier admission, error, confirmation, stressed, sensitive, roast-me-anyway, frustration aimed at an object, company mode, on-behalf drafting, Hinglish, compliment, insult, voice first sentence, real win, night nudge, night decline, night accept, morning brief. About 10 are held-out (no exemplar resembles them).
- rubric.md: pairwise judge, persona.v2 vs the current contract; scores truth, usefulness, human, funny, appropriate, generality; plus the spec §11 hard fails.
- run.py: real brain or scripted mock only, never Echo; fixed generation temperature; judge at temperature 0; randomized A/B order; results to results/; exposes run_persona_gate() for Agent 20; does not touch eval_gates.py.
- mine_exemplars.py: offline only; writes to a proposal file, never to exemplars.yaml.
- This phase: a 3-scenario dry run on the scripted mock only. No paid runs.

## 9. Cutover proposals (write, don't apply)
Write personality/proposals/0001-persona-v2-cutover.md with exact diffs for after the FLEET_LAW §13 override is recorded: SPEECH_STYLE_INSTRUCTIONS delegating to persona.core behind the flag (keeping the existing rollback); the lock tests' deliberate-update path (which pinned strings change and why); retiring the competing identity at cognitive/context.py:98 (kernel owner executes); the contract-ordering fix for the life-agency block at gateway/service.py:94-104 (its owner executes).

## 10. Handoffs (FLEET_LAW DEPENDENCY NOTE convention)
Name build_persona_block(ctx) and persona_guard(text, ctx) as the interfaces in each:
1. Owner: record the §13 override, scoped to the character layer.
2. Kernel/brain owner: accept persona_block in compile_context before TOOL ROUTING; call persona_guard on spoken before emitting; pass the NOW-line signals you already hold.
3. Agent 16 (filter): one ordered stage, persona_guard, inside enforce_persona after the guardrails and before the length trim; its flags go to the existing ledger.
4. Agent 20 (evals): a run_persona_gate() slot.
5. Shared config owners (config.py, .env.example, docs/ENVIRONMENT.md): append a CLI-6-marked block with the settings from section 1.
6. Voice owner (gemini_live.py:818, webrtc_live.py:369): build the system instruction with build_persona_block(channel="voice"|"phone") behind the flag. Live voice has no post-filter.
7. Agent lane: a run_night_shift(plan) entry point — check forbidden before every tool call (hard deny), work only in the workspace, queue confirmation-class actions, checkpoint, respect stop conditions, emit a MorningBrief with receipt references.
8. Scheduling/notifications owner: deliver the brief on the first owner message after 06:00, or at wake_time if set. Overnight, push only for "on fire" events (risk of data loss, runaway process).
9. Thread-state owner: persist per thread serious_latch_turns_left, recent_joke_flags, last_joke_meta, night_state; persist the learning weights per owner.

## 11. Stop and report
Stop when sections 1–10 are done and all tests pass. Write personality/_recon/PHASE1.md: files created; test results; measured latency and token sizes (text and voice); learning-simulation results; reaction-classifier precision; Night Shift validator coverage; which path serves /v1/chat by default, with evidence; handoffs sent; proposed spec changes; blockers.
Do not flip PERSONA_ENABLED. Do not start Phase 2.
