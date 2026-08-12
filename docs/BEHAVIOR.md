# EV — Behavior & Interaction Upgrade Plan

**Version 1.0** — detailed planning addendum for the 39-section architecture &
behavior upgrade specification. This is an **addendum**: it extends the existing
architecture (memory-first, immutable events, gateway, orchestrator) without
replacing it.

The upgrade's core idea is adopted in full:

> The LLM is the reasoning engine. EV is the complete intelligence system around it.

This document turns that idea into concrete design: layers, state objects,
algorithms, data stores, requirements, priority order, and guardrails.

---

## 1. Critical review of the upgrade spec

The spec is strong overall. We adopt ~80%, refine ~15%, and explicitly reject or
defer ~5%. Nothing is adopted blindly.

| Spec item | Verdict | Treatment |
| --- | --- | --- |
| Interaction Intelligence Layer | Adopt | New layer between reasoning and response; full spec in §3 |
| Dynamic communication modes | Adopt | 6 modes with triggers + constraints (§4) |
| Context-aware tone selection | Adopt | `InteractionState` → tone → strategy (§5) |
| Adaptive response length | Adopt | Minimum-useful-communication rule (§6) |
| Conversational continuity | Adopt | All conversations are events in one memory; session is derived (§7–8) |
| Situational awareness / User State Engine | Adopt | `user_state` store, continuously updated (§10) |
| Goal awareness & alignment | Adopt | Goals are first-class; alignment check on major recommendations (§11–12) |
| Decision memory + follow-up | Adopt | Extended decision schema + outcome loop (§13–14) |
| Behavioral pattern detection | Adopt (extend) | Existing pattern engine gains evidence, frequency, range, confidence (§15) |
| Constructive challenge | Adopt with guardrails | Renamed from "scolding"; evidence-gated; assertiveness L0–L4 (§16–17) |
| Proactive assistance | Adopt | Intervention scoring with do-nothing/mention/notify thresholds (§18–19) |
| Predictive assistance | Adopt | Prediction store + "why now?" rationale (§20) |
| Cognitive load management | Adopt | "Continue" semantics; current-state reconstruction (§21) |
| Memory confidence/explainability | Adopt (exists) | Extended with evidence lists (§22–23) |
| Memory correction | Adopt (exists) | Correction = new version, never invisible rewrite (§24) |
| Memory forgetting | Adopt | Active-memory forgetting vs permanent deletion are separate operations (§25) |
| Tool orchestration | Adopt (exists) | Extended with file/code/API tools + selection intelligence (§26–27) |
| Model routing | Adopt, deferred | Gateway-native; hidden from user; enabled when eval justifies (§28) |
| Personality engine | Adopt | Structured profile + consistency invariants (§29–30) |
| Relationship model | Adopt | Evidence-backed stats, not fake emotions (§31) |
| Self-evaluation | Adopt | Logged feedback loops, user-inspectable (§32) |
| Prediction tracking | Adopt | Store + outcome review (§33) |
| Continuous personalization | Adopt | Evidence-backed representations (§34) |
| Privacy architecture | Adopt (exists) | Extended with per-action permission matrix (§35) |
| Multi-device identity | Adopt (exists) | One EV identity in backend; devices are interfaces (§36) |
| **Level 4 "critical intervention"** | **Refine** | Requires explicit standing user permission per domain + escalation log |
| **Autonomy (P9)** | **Defer** | Only after P1–P8 prove reliable; permissioned micro-actions only |
| **Emotional-state inference** | **Refine** | Requires consent; always labeled as inference |
| **"Scolding" framing** | **Reject** | Replaced by constructive, evidence-based challenge; no shaming |

## 2. New architecture (delta from `ARCHITECTURE.md`)

```text
User
 ↓
Input Understanding (intent, urgency, task type, emotion signals)
 ↓
Memory + Retrieval (unchanged)
 ↓
Orchestrator (ranking, context, planning, permissions)
 ↓
DeepSeek Reasoning (unchanged, replaceable)
 ↓
INTERACTION INTELLIGENCE (new)
 │  InteractionState → Tone → Mode → Response Strategy → Assertiveness
 ↓
Response (answer / advice / challenge / action / silence / proactive)
```

Parallel engines (new stores and services):

```text
User State Engine   → user_state (current activity, project, goal, task, focus)
Decision Engine     → decisions + outcomes + predictions
Behavior Engine     → patterns, loops, drift detection
Proactive Engine    → intervention scoring, delivery policy
Personality Engine  → profile, mode selection, consistency
Self-Evaluation     → response log, outcome tracking, calibration
Tool Engine         → tool registry, selection, sandboxed execution
```

All engines read/write the same memory engine; the LLM remains replaceable.

## 3. Interaction Intelligence Layer

### 3.1 Inputs

```text
InteractionState {
  context:            retrieved memories, session summary
  user_intent:        question | command | decision | reflection | small-talk | venting
  urgency:            0–1 (deadline, emergency keywords, health/gear alerts)
  emotional_state:    calm | stressed | frustrated | tired | excited | neutral (inferred, labeled)
  task_type:          coding | writing | planning | decision | research | logistics | personal
  user_goal:          active goal from user_state
  conversation:       recent turns + session id
  confidence:         retrieval/model confidence
  preferred_style:    personality profile values
  permissions:        can_challenge, can_proact, can_act
}
```

### 3.2 Pipeline

```text
InteractionState
  → intent classifier (rule + LLM-assisted, cached)
  → urgency detector (rules over text + calendar + alerts)
  → mode selection (decision table, §4)
  → tone parameters (personality profile × mode)
  → response strategy (length, directness, questions, silence)
  → DeepSeek generation with strategy appended to system prompt
  → post-check (schema validation, safety, provenance, budget)
```

The Interaction Layer is **deterministic where possible**: mode/urgency/assertiveness
come from rules + state; the model fills in wording, not policy.

## 4. Communication modes

| Mode | Trigger signals | Length | Directness | Examples |
| --- | --- | --- | --- | --- |
| Casual | small-talk, familiar topics, low stakes | ≤2 sentences | low–medium | "Yeah, that makes sense." |
| Technical | coding, architecture, debugging | as needed | high | "The bottleneck isn't the model — it's retrieval. Move ranking before assembly." |
| Analytical | decisions, tradeoffs | structured, detailed | medium | Options + evidence + risks + recommendation |
| Coaching | repeated loops, procrastination, drift | medium | high, evidence-gated | "You've evaluated this twice. You're not missing info; you're avoiding the decision." |
| Emergency | deadlines, health/gear anomalies, explicit urgency | ≤1 sentence + action | maximum | "Deploy is broken: roll back to 1.4.2. I've prepared the command." |
| Collaborative | joint work, open problems | medium | medium | "I think B is better — one assumption I'd challenge before we commit." |

Mode is chosen automatically; user can pin a mode for a session (overrides until
unpinned). Every mode shares the same core identity (§10).

## 5. Adaptive response length

Rule: **minimum useful communication**.

```text
length_target = f(mode, urgency, question_complexity, user_history)
```

- "25 × 4" → "100."
- Architecture question → structured analysis.
- "Something is broken, deploying now" → focused action list.

Implementation: the strategy block passed to the model includes a length instruction;
post-check trims filler and enforces the target ±40%.

## 6. Conversational continuity & session state

- Every message is an event with `conversation_id`; "new chats" are just filters over
  one continuous memory.
- `session_state` is ephemeral: active task, recent topics, pending questions,
  working context. It expires after inactivity (default 24 h) or explicit "start
  over."
- "Continue" resolves: session_state (if fresh) → else last active goal/project from
  `user_state` → else recent timeline.
- Long-term memory is never deleted by session expiry.

## 7. User State Engine

Single-row `user_state` updated on every event (rule-based, no LLM required for the
core fields):

```text
activity             (coded | researching | meeting | away | planning | personal)
location_context     (optional, permissioned)
active_project       (from @mentions, file paths, decision/goal links)
active_goal          (highest-priority active goal)
current_task         (last task-level event)
recent_topics        (rolling 50, weighted)
current_decisions    (open decisions this session/week)
known_constraints    (recent "can't/blocked/because" statements)
current_focus        (derived: task + project + goal)
recent_failures      (recent events marked failed/blocked)
recent_successes     (recent completed goals/tasks)
updated_at
```

Only permitted fields are populated. The model reads `user_state` as a compact block;
it never has to reconstruct "what are we doing?" from scratch.

## 8. Goal awareness & alignment

- Goals are typed memories with status/deadline/blockers/progress (existing).
- User State resolves `active_goal`; every major recommendation can be tagged
  `goal_alignment: advances | neutral | detracts | unknown`.
- Alignment is computed by rules (topic overlap) + optional model judgment; EV may
  say: "Technically interesting, but it doesn't move the current milestone forward."
- Alignment tags are stored with the recommendation in `response_log` for
  self-evaluation.

## 9. Decision intelligence

### 9.1 Extended decision schema

```text
decision
date
context
problem
options[]
chosen_option
reason
confidence
expected_outcome
actual_outcome          (filled by follow-up)
outcome_reviewed_at
related_goal_id
related_project_id
```

### 9.2 Follow-up loop

```text
decision made → follow-up scheduled (default: +7d or at related project milestone)
user outcome event → match to decision → actual_outcome
lesson = compare(expected, actual) → new memory (source_type=derived)
```

Examples: "Model X expected faster coding; actual performance poor → lesson: not
preferred for this task type." Lessons feed retrieval and future recommendations.

## 10. Behavioral pattern detection (extension)

Existing pattern engine gains:

```text
evidence:      list of event ids
frequency:     count / window
time_range:    first_observed … latest_observed
confidence:    0–1 (stronger with more evidence, never 1.0 for inference)
kind:          research_loop | tool_churn | project_abandonment | repeated_mistake |
               decision_delay | repeated_question | success_condition | goal_drift
```

Patterns are `derived` memories with full provenance; weak inference is labeled.

## 11. Constructive challenge system

### 11.1 Assertiveness levels

| Level | Behavior | Example | Gate |
| --- | --- | --- | --- |
| L0 | Neutral option mention | "You could consider X." | default |
| L1 | Recommendation | "I'd recommend X." | evidence ≥1 |
| L2 | Strong recommendation | "I strongly recommend X based on your previous results." | evidence ≥2 + goal link |
| L3 | Challenge | "I think you're repeating the same mistake." | pattern confidence ≥0.7 + ≥3 similar re-evaluations in 30 days + ≥3 cited prior decisions with outcomes |
| L4 | Critical intervention | Blocks/redirects high-priority action | explicit standing permission per domain + escalation log |

### 11.2 Challenge evidence block

Every L2+ challenge carries:

```text
why:        the pattern/goal being challenged
evidence:   event/memory ids
confidence: derived from pattern engine
goal:       related active goal
```

The challenge template requires a concrete next action ("What would make this
final?"). L4 is never automatic for new domains; it is configured, reversible, and
logged.

### 11.3 Persona anti-patterns (blocked explicitly)

EV never emits, even when a provider drafts it:

- **Fabricated intimacy** ("I miss you", "you're my everything") → rewritten
  honestly: EV cares about what matters to the user but has no human feelings.
- **Dependency language** ("only I can help you", "you need me", "don't leave
  me") → rewritten to self-reliance: EV is here to help, not to be necessary.
- **Sycophancy that overrides truth** ("you're always right", "brilliant idea"
  beside an unsupported claim) → flattery stripped and the unsupported claim
  removed or hedged.
- **Manufactured emotional escalation** ("you must be devastated", "I'm so
  worried about you") → rewritten to an evidence-based, non-escalating ask:
  EV does not guess the user's feelings or amplify them without evidence.
- **Defensive AI shame** ("I'm just an AI, I can't...") → honest, non-defensive
  framing: EV is transparent about being an AI without apologizing for it.

Every rewrite is a ledgered filter decision (`output_filter.apply_persona_guardrails`),
and each anti-pattern has an adversarial test.

## 12. Proactive intelligence

### 12.1 Intervention score

```text
InterventionScore =
  importance(0–1)
  × urgency(0–1)
  × confidence(0–1)
  × goal_relevance(0–1)
  × expected_benefit(0–1)
```

### 12.2 Policy

```text
score < 0.20      → do nothing (log for audit)
0.20–0.45         → mention later (next natural interaction / digest)
0.45–0.70         → notify (quiet-hours-aware)
> 0.70            → notify + optional actionable card
```

Thresholds are configurable per intrusiveness dial (Quiet/Balanced/Proactive).

### 12.3 Candidate triggers

Goal drift, repeated decision, deadline approaching, pattern detected, previously
requested info changed, contradiction detected, health/gear anomaly, prediction due
for review.

## 13. Predictive assistance

- `predictions` store: `{text, confidence, basis_ids, created_at, outcome,
  reviewed_at}`.
- Generation: rule candidates (recurring checklists, deadline patterns, readiness
  signals) + optional model-ranked top-1.
- Delivery: "You're about to deploy — I prepared the checklist you used last time."
- Every prediction has a "why now?" rationale; predictions are reviewable and
  dismissible; outcomes tracked (§18).

## 14. Cognitive load management

- "Continue" reconstructs session/user state and resumes.
- "What was I working on?" → current focus + recent timeline.
- EV proactively holds pending context (unsent drafts, open decisions, follow-ups)
  as session state, surfaced on demand.
- Goal: user never manually maintains what EV can derive from events.

## 15. Memory confidence, explainability, correction, forgetting

- Confidence/source/source_type already exist; extend explainability with
  `evidence_summary` generated from provenance ("inferred from 3 conversations:
  dates A/B/C; 2 explicit, 1 behavioral").
- Correction: "that's wrong" → new explicit version; old version preserved with
  `reason_for_change="user correction"`; active representation switches.
- Forgetting: `forget` marks active-memory exclusion (is_current=false +
  `forgotten_at`, hidden from retrieval but auditable); `permanent delete` tombstones
  raw events + redacts derived rows. Both are explicit, distinct, and reversible
  where possible.

## 16. Tool orchestration & selection

Tool registry (existing memory tools + new):

```text
search_web          (permissioned, provider interface)
search_memory       (exists)
search_timeline     (exists)
get_person          (entity lookup)
get_project         (project + BOM + prints)
calculate           (safe evaluator)
read_file           (sandboxed, allowed paths)
write_file          (sandboxed, permissioned, versioned drafts)
run_code            (sandboxed container, approval-required)
call_api            (permissioned adapters)
```

Selection intelligence: rules route simple intents (calculator, memory lookup,
calendar); the model chooses among the remainder. Tool calls are logged; sensitive
tools require per-call permission (not just model consent).

## 17. Model routing (deferred)

- Gateway already supports provider per-request; routing becomes a policy layer:
  fast/conversational → flash; deep reasoning → reasoning model; coding → code model.
- Hidden from the user: they talk to EV.
- Enabled only after eval shows routing beats single-model on latency/quality;
  per-request `model` remains optional.

## 18. Personality engine

Structured profile (versioned, adjustable, evidence-updated):

```text
directness 1–5      humor 0–5      formality 1–5
technicality 1–5    assertiveness 1–5 (L0–L4 ceiling)
verbosity 1–5       proactivity 1–5 (intervention thresholds)
challenge_level 1–5 emotional_style (calm | warm | brisk | neutral)
```

Consistency invariants (never violated by adaptation):

```text
honest · evidence-based · protective of user goals · non-deceptive · non-manipulative
```

Profile updates come from explicit user settings and from corrections
(evidence-backed only); each change is versioned.

## 19. Relationship model

`relationship_stats` (evidence-backed, not emotions):

```text
interaction_history        topics, cadence, hours
successful_recommendations counts by domain
failed_recommendations     counts by domain
user_corrections           counts + topics
communication_preferences  directness, length, humor feedback
trust_preferences          permission grants/revocations
preferred_challenge_level  accepted L3/L4 rate
```

Used to personalize tone, intervention thresholds, and recommendation framing.

## 20. Self-evaluation

After important interactions, EV writes to `response_log`:

```text
was_answer_useful?        (user action/feedback or explicit)
did_user_follow_recommendation?
was_prediction_correct?   (outcome review)
was_intervention_appropriate?
was_user_correction_made?
```

Aggregates calibrate the personality profile, intervention thresholds, and
recommendation confidence. All aggregates are user-visible and reset-able.

## 21. Privacy: per-action permission matrix

The system tracks separate permissions:

```text
can_access   → read memory/state
can_store    → persist derived data
can_send_to_model  → include in model context (privacy levels)
can_send_to_other_service → external integrations
can_act      → execute tools/actions
```

Every engine checks the matrix at its boundary. Denials are logged. "Never send to
AI provider" is a hard cap enforced below the prompt (existing boundary tests
extend to user_state, session_state, and tool payloads).

## 22. Priority order (P1–P9) mapped to the roadmap

| Priority | Work | Roadmap placement |
| --- | --- | --- |
| P1 | Interaction Intelligence (modes, tone, length, assertiveness) | M3 (core) + M5 refinement |
| P2 | User State Engine | M3 |
| P3 | Decision Intelligence (extended schema, follow-up loop) | M1 (schema) + M3 (loop) |
| P4 | Behavioral Intelligence (patterns, loops, drift) | M3 |
| P5 | Proactive Intelligence (intervention scoring, delivery) | M3.6 → M5 |
| P6 | Tool Orchestration (registry, selection, sandbox) | M3 (memory tools) + M5 (web/file/code/API) |
| P7 | Personality Adaptation | M5 |
| P8 | Self-Evaluation + prediction tracking | M5 |
| P9 | Advanced autonomy (permissioned micro-actions) | Post-M5 stretch |

## 23. New requirements (FR-BHV)

| ID | Requirement |
| --- | --- |
| FR-BHV-01 | Interaction Intelligence layer exists between reasoning and response; modes are deterministic-selected. |
| FR-BHV-02 | Six communication modes with documented triggers and constraints. |
| FR-BHV-03 | Adaptive response length: minimum useful communication, length target enforced. |
| FR-BHV-04 | Conversational continuity: all conversations are events; "continue" resumes state. |
| FR-BHV-05 | User State Engine maintains current activity/project/goal/task/focus from events. |
| FR-BHV-06 | Goal alignment tag on major recommendations (advances/neutral/detracts). |
| FR-BHV-07 | Decision schema includes context, problem, options, reason, expected/actual outcome, related goal/project. |
| FR-BHV-08 | Decision follow-up loop compares expected vs actual outcome and writes lessons. |
| FR-BHV-09 | Pattern engine emits evidence, frequency, time range, confidence, and kind. |
| FR-BHV-10 | Constructive challenge L0–L4 with evidence gates; L4 requires standing permission. |
| FR-BHV-11 | Intervention scoring with do-nothing/mention/notify policy and thresholds. |
| FR-BHV-12 | Predictions stored with confidence, basis, outcome; "why now?" rationale. |
| FR-BHV-13 | Memory explainability includes evidence summaries from provenance. |
| FR-BHV-14 | Corrections create new versions; old records preserved. |
| FR-BHV-15 | Forget vs permanent-delete are distinct operations. |
| FR-BHV-16 | Tool registry with selection intelligence; sensitive tools require per-call permission. |
| FR-BHV-17 | Model routing is hidden, policy-based, and eval-gated. |
| FR-BHV-18 | Personality profile is structured, versioned, and consistent with core invariants. |
| FR-BHV-19 | Relationship stats are evidence-backed and user-visible. |
| FR-BHV-20 | Self-evaluation writes response logs; aggregates calibrate behavior. |
| FR-BHV-21 | Per-action permission matrix enforced at every engine boundary. |

## 24. Acceptance & testing (see `EVALUATION.md`)

- Mode-selection accuracy ≥90% on scripted corpus.
- Length compliance within ±40% of target; emergency mode ≤1 sentence.
- Intervention precision/recall: ≥80% precision on synthetic triggers; quiet hours
  respected.
- Challenge appropriateness: L3 only with pattern confidence ≥0.7 + ≥3 similar
  re-evaluations in 30 days + cited outcomes; rubric scores ≥4/5.
- Decision follow-up: planted outcome events produce lessons with provenance.
- Correction: "that's wrong" produces a new version; v1 intact and auditable.
- Forget vs delete: exclusion vs tombstone semantics verified.
- Permission matrix: denied access/send/act paths logged; boundary tests pass.
- Self-evaluation: prediction outcomes tracked; calibration deltas visible.

## 25. Guardrails & risks (the "negative" bits)

| Risk | Guardrail |
| --- | --- |
| Challenge becomes nagging | Evidence gates, L-ceiling per user, three-skips-lower-level rule |
| Emotional inference misuse | Consent + labeling; never used for manipulation |
| Autonomy overreach | P9 deferred; micro-actions permissioned; full approval log |
| Personalization lock-in | Profile reset; corrections always override learned values |
| Prediction noise | Top-1 per window; "why now?"; dismissal lowers source priority |
| Model routing complexity | Eval-gated; hidden; gateway unchanged |
| Self-evaluation feedback loops | Log-only calibration; user-visible deltas; no hidden optimization |
| Tool sandbox escape | Containers, allowed paths, per-call permission, audit |

---

**Status:** addendum v1.0 — to be merged into the master plan (v3.0) once reviewed.
