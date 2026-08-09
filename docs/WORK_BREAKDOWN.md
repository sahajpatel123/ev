# EVIE — Complete Work Breakdown (Every Factor)

**Purpose:** every factor that must exist for EVIE to work as a lifelong,
voice-aware, 24/7 personal intelligence. Each factor lists what we plan and the
direction we'll take. Status: **Built** (implemented + tested), **Partial**
(core exists, gaps remain), **Design** (architecture written, not built),
**Future** (roadmap only).

---

## 1. Memory & data foundation

**Domain essence:** This is the soul of EVIE. Raw immutable events capture
everything that happens or is told to her; derived memories turn that raw stream
into facts, decisions, goals, preferences, patterns, and lessons; versioning
guarantees the past is never rewritten; provenance makes every answer explainable;
correction and forgetting give the user final control. The vision is a perfect
transcript of a life that never lies, never loses the past, and can rebuild any
conclusion from its sources — so "why do you know that?", "what was I thinking in
March?", and "delete that from your memory" all work with equal ease. Success
means every derived table can be dropped and regenerated from events into an
equivalent state, and every memory carries the evidence trail that earned it.

### 1.1 Immutable raw event store — **Built**
Every input (text, voice, image, file, live event) is an append-only event with
`occurred_at`, `ingested_at`, source, type, content, privacy level, and a
content hash. Direction: keep it the single source of truth; all derived data
must remain rebuildable from it. No code path may ever update or physically
delete a raw event.

### 1.2 Derived memory system — **Built**
Facts, decisions, goals, preferences, observations, patterns, summaries, and
lessons are derived, typed, scored (importance/confidence), and provenance-linked
to events. Direction: grow extraction quality (rules → LLM-assisted), and keep
the rebuild-from-events invariant tested.

### 1.3 Versioning & time travel — **Built**
A changed memory creates a new version in the same version group with
`valid_from`/`valid_until`, `supersedes`/`superseded_by`, and a reason. Direction:
make as-of queries ("what was I thinking in March?") first-class across all
endpoints and add a time-travel UI.

### 1.4 Entities & relationships — **Built**
People, places, projects, topics are canonical entities with aliases; memory-to-
entity links carry role/weight; typed relationships are time-valid. Direction:
improve entity resolution (dedup, merge, canonicalization) and consider a graph
layer only if relational queries measurably hurt.

### 1.5 Contradiction detection — **Built**
Conflicting observations create open conflict records instead of silent
arbitration; resolution creates an explicit superseding memory. Direction: extend
to facts/preferences/decisions, and surface conflicts in the filter's grounding
audit.

### 1.6 Provenance & audit ("why do you know that?") — **Built**
Every memory traces to source events; audit shows version chain, conflicts, and
access log. Direction: make the output filter emit memory-linked claims so
provenance becomes part of every answer automatically.

### 1.7 Memory correction, forgetting, restoration — **Built**
Correction creates a new version preserving history; forgetting hides from active
retrieval reversibly; restore brings it back. Direction: add batch corrections
and a "what did I forget?" review surface.

### 1.8 Access log & read/write audit — **Built**
Every read/write/export/delete is logged with actor, endpoint, resource, and
request id. The log itself is exportable via `GET /v1/compliance/access-log`
(paged, actor/action/time filters, owner-trusted, read audited) and pruned by
the retention sweep (`EV_RETENTION_ACCESS_LOG_DAYS`). `GET
/v1/compliance/anomalies` runs rule-based detection for deletion spikes,
export/backup bursts, and repeated failed actions; direction: adaptive
thresholds and ML baselines.

### 1.9 Export, import, deletion — **Built (partial)**
Full export bundles events, memories, entities, relationships, conflicts;
tombstone delete redacts derived rows. Direction: implement import/restore round-
trip and scheduled encrypted backups (see §13).

### 1.10 Long-horizon consolidation — **Built**
Daily/weekly/monthly period summaries are derived memories
(`app/services/consolidation.py`, `POST /v1/consolidate`) with deterministic
reruns, versioned supersession, provenance links, and rebuild support.

### 1.11 Storage of voiceprints & biometrics — **Built**
Voice samples are hashed and discarded; encrypted, versioned voiceprint
templates (Fernet at rest) live in `voice_enrollments` + `voice_prints` with
re-enrollment chains, rollback, revocation, and data-subject deletion.

---

## 2. Single conversation & context

**Domain essence:** There are no chat #1, #2, #3 — there is one continuous
relationship. A single default thread holds every message forever, and an
ephemeral state layer remembers what we are working on right now, what questions
are open, and what context matters. The vision is that the user never has to
reintroduce themselves or their work: "continue" just works, on any device,
mid-thought. The 1M-token window is treated as a scratch workspace — a rolling
summary, recent turns, retrieved memories, then progressive deep dives — so
"remember my whole life" is satisfied by the memory store plus smart loading,
not by stuffing a lifetime into one prompt. Success is measured by zero context
restarts: the user always feels like they are talking to the same EVIE.

### 2.1 One lifelong conversation thread — **Built**
`conversation_threads` has exactly one default thread; chat without an id
resolves to it, and the response always returns the same id. Direction: keep the
invariant "no new chat" and extend it to voice sessions.

### 2.2 Ephemeral conversation state — **Built**
Per-thread focus, recent topics, pending questions, and working context live in
`conversation_states`; reset clears state, never history. Direction: add
expiration, merge rules, and voice-session state.

### 2.3 Continuous history in prompt — **Built**
The last ~10 turns are injected as a continuous window so "continue" works
without restating context. Direction: make the window adaptive (importance and
recency weighted) and compress older turns into rolling summaries.

### 2.4 1M-token window strategy — **Built**
The window is a scratch workspace, not a life-dump. `app/context/compiler.py`
implements the ContextCompiler: deterministic per-request window planning
(strategy → user state → live context → rollup → retrieved memory → history →
open questions) with a real-time budget monitor report (per-section tokens,
included/dropped items, remaining budget). The chat assembler delegates to it
and depth profiles scale the budget. Direction: expose the plan in chat
responses and add progressive tool-loaded deep dives.

### 2.5 Whole-life recall — **Partial**
"Remember my whole life" = memory store + hierarchical retrieval, not a lifetime
in context. `GET /v1/recall/week` reconstructs any past week: raw events,
end-of-week versioned memory state (validity-window time travel), weekly
period-summary consolidation, and decisions/goals with provenance. Direction:
month-over-month "how has my thinking changed" comparisons and a recall UI.

---

## 3. Intelligence filter

**Domain essence:** The provider is a brilliant but generic brain; the filter is
what makes its output EVIE. Every byte travelling to the provider is shaped by
the input filter (identity, privacy, intent, state, memory, context), and every
byte coming back is refined by the output filter: structure validated, claims
grounded in real memory, persona and tone enforced, safety applied, and a critic
loop polishing weak drafts. The vision is that EVIE's quality, honesty, and voice
live in the filter rather than in any single model — so DeepSeek can be swapped
without EVIE changing. Success means no ungrounded personal claim survives, every
HUD contract renders perfectly, every filter decision is recorded in a ledger,
and the filter measurably gets better from the user's corrections.

### 3.1 Input filter — **Built**
Every inbound utterance passes IdentityGate, InputGuard (injection/PII/privacy
caps), intent/state compile, MemoryBroker, and ContextCompiler before reaching
the provider. Implemented in `app/filter/input_filter.py` and wired into the
chat pipeline: unverified speakers block, injection attempts block/flag,
credentials are redacted from the provider-bound message with
`never_send_to_model` privacy, and every decision is ledgered. Voice-enrollment
identity remains future work (see §5).

### 3.2 Output filter — **Built**
Structural validation → grounding audit → persona/style → safety → critic loop →
finalize. Deterministic stages plus an optional provider-backed critic are
implemented in `app/filter/output_filter.py` / `app/filter/critic.py` (HUD
contract validation/repair, claim grounding audit, persona/length, safety
redaction, rule-based critic loop, honest fallback). Semantic/embedding claim
verification remains a future refinement.

### 3.3 Grounding audit — **Built**
Claims are checked against the memories actually in context (entity/date/number
overlap, dates, significant tokens); unsupported personal claims are removed
before the response is finalized, and the audit is reported per claim.
Semantic-similarity verification and a seeded eval corpus are future additions.

### 3.4 Persona & style enforcement — **Built**
Mode, length ±40%, directness, assertiveness ceiling, challenge-evidence gating,
and urgency conciseness are enforced after generation with rule-based checks;
a learned voice profile is future work.

### 3.5 Safety & privacy filter — **Built**
Output-side secret/PII redaction, toxicity, manipulation, dependency nudging,
jailbreak leaks — deterministic detectors implemented; a local model for
semantic checks is future work.

### 3.6 Critic & refine loop — **Built**
LLM-as-judge rubric (grounding, persona, actionability, honesty, contract) with
max two refinement iterations and staged trust. A deterministic rubric judge
and a provider-backed critic through the neutral gateway
(`app/filter/critic.py`, gated by `EV_FILTER_CRITIC_ENABLED` and staged modes)
are implemented; a separate local critic for privacy and a learned feedback
model after ledger data accumulates remain future work.

### 3.7 Filter ledger — **Built**
Every filter decision (draft, edits, scores, flags, iterations, cost) is
recorded in `filter_ledger` with stage/action/severity/detail/envelope hash,
exposed via `/v1/filter/ledger` and `/v1/filter/ledger/aggregate`, and ready to
feed thresholds and the self-evaluation engine.

### 3.8 Filter-as-API & replay tests — **Built**
`POST /v1/filter/evaluate` runs the input filter on a message and the output
filter on a draft (or the full provider pipeline when no draft is given),
records everything to the ledger, and is covered by
`tests/test_intelligence_filter.py`. Snapshot replay of historical drafts is a
future addition.

### 3.9 Streaming refinement — **Design**
Buffered-final first: stream raw text, then emit a `refined` event; chunk-filter
later. Direction: define client protocol (replace vs. append) and latency
budgets.

---

## 4. Provider & models

**Domain essence:** DeepSeek V4 Flash is the brain — general reasoning, no
specialized EVIE knowledge — and the gateway is the neutral socket it plugs into.
The vision is a swappable brain: today DeepSeek, tomorrow a deeper reasoning
model, a coding model, or a local model, all hidden behind EVIE's identity. The
gateway must carry a full envelope (strategy, memories, request id) so the filter
can audit every call, validate tool invocations before they execute, and route
simple requests to fast models and hard problems to deep ones when evaluation
proves routing wins. Success means changing the provider is a configuration
change, not a personality change.

### 4.1 Provider gateway (DeepSeek V4 Flash) — **Built**
Chat/tools/models endpoints with echo/mock providers for offline dev; DeepSeek is
the default brain. The gateway now enforces envelope contracts (strategy,
memories, request id, metadata) on every call and persists an append-only
`model_calls` audit record (provider, model, latency, usage, envelope,
tool-validation outcome, errors).

### 4.2 Provider swappability — **Built**
The gateway is model-agnostic by design. Direction: add a model-routing policy
(fast vs. deep) hidden behind EVIE, gated by evaluation.

### 4.3 Tool-call validation — **Built**
Tool registry + dispatcher + selection exist; model tool calls are pre-validated
by the gateway before anything executes (Invocation-Refiner pattern): unknown
tools and malformed arguments are rejected, missing optional arguments with
declared defaults are rectified, and sensitive tools require an explicit
permission gate (`allow_sensitive_tools`).

### 4.4 Local models (future) — **Future**
2B-class local critic for style/safety, on-device STT/TTS, wake word models.
Direction: evaluate edge hardware and power budgets before committing.

### 4.5 Embeddings — **Built**
Hash provider for tests; OpenAI-compatible HTTP provider for production; pgvector
column ready. Direction: pick a dedicated local embedding model and an eval
corpus for retrieval quality.

---

## 5. Voice & speech

**Domain essence:** This is how EVIE becomes a presence rather than an app. An
always-on, low-power wake engine listens for "EVIE"; speaker verification makes
sure only the owner's voice unlocks it; anti-spoofing stops recordings and
synthetic voices; ASR turns speech into text; TTS gives EVIE a natural voice
that can carry urgency, warmth, and brevity. The vision is a "Hey Siri"-class
experience that is private: wake, verify, listen, understand, act, reply, and a
30-second follow-up window before returning to silent idle. Success means the
wake word never fires for strangers, replay attacks fail, and voice feels as
natural as talking to a person who knows you.

### 5.1 Wake word engine ("EVIE") — **Built (dev)**
Deterministic wake engines in `app/voice/wake.py` with multi-stage
low_power/burst semantics; production Sensory/AON1100-class model is a
provider swap behind the same contract.

### 5.2 Speaker verification (owner-only) — **Built**
Enroll voice samples → encrypted, versioned voiceprints; every wake/verify
path checks the owner; unknown voices get a polite refusal and the session
ends. Remote encoders require explicit regional-policy approval.

### 5.3 Anti-spoofing / liveness — **Built**
`app/voice/anti_spoof.py` implements single-use challenge nonces, audio
fingerprint replay rejection, and a liveness gate; failed liveness ends the
session.

### 5.4 Speech-to-text (ASR) — **Built**
`app/voice/asr.py` supports the deterministic dev transcriber and an
OpenAI-compatible provider; transcripts are recorded as sensitive events.

### 5.5 Text-to-speech (TTS) — **Built**
`app/voice/tts.py` synthesizes with urgency/warmth/brevity styles via the dev
or OpenAI-compatible provider; audio refs are returned to clients.

### 5.6 Voice session lifecycle — **Built**
Wake → verify → listen → process → respond → 30s follow-up → idle is
implemented in `app/voice/lifecycle.py` with session timeouts, replay guards,
and consent gating on wake/verify.

### 5.7 Voice enrollment UX — **Built**
The web workbench now has a voice enrollment panel: consent grant, 5-sample
microphone capture with progress feedback, enroll, status, and
revoke/delete, backed by the audited APIs.

---

## 6. 24/7 runtime & devices

**Domain essence:** EVIE is not a request-response server; she is a living
runtime. Devices become ears — the Mac, the iPhone, future glasses — each running
a low-power listener, with the closest or most capable device winning the wake.
A central state machine moves from idle to verifying, awake, processing,
responding, and follow-up; queues, heartbeats, and dead-letter handling keep it
alive; the action router turns commands into approved actions; quiet hours and
the attention budget keep her from becoming a nag. The vision is Siri-like
availability with home-lab ownership: always on, always reachable, never
annoying, and observable — you can see every pulse of the system. Success means
wake-to-reply in seconds on any device and zero silent failures.

### 6.1 Always-on runtime state machine — **Built**
IDLE → VERIFYING → AWAKE → PROCESSING → RESPONDING → FOLLOW-UP → IDLE, with
timeouts and quiet hours, is implemented as a centralized runtime with
`runtime_sessions`, a legal-transition engine, `POST /v1/runtime/wake`,
`POST /v1/runtime/verify`, `POST /v1/runtime/transition`, and
`GET /v1/runtime/status`. The always-on daemon (`workers/runtime_daemon.py`,
`runtime_daemon` compose service) expires stale sessions, re-enqueues retrying
dead letters, builds the quiet-hours digest, and runs health checks; per-device
listener agents (Python `clients/device_listener.py`, Swift `RuntimeListener`)
drive these endpoints. Direction: notification delivery for digests.

### 6.2 Device fleet as "ears" — **Built**
Fleet status, gear telemetry, task dispatch, and wake arbitration exist:
`POST /v1/runtime/wake` scores online wake-capable devices by signal, battery,
proximity, and heartbeat recency, gates quiet hours and focus mode, and the
winner drives the state machine. Direction: audio-capture capability
negotiation and on-device wake-word engines (Sensory/AON1100-class).

### 6.3 Queue & background workers — **Built**
Redis/RQ ingestion pipeline with sync fallback, plus dead-letter records
(`dead_letters`) with retry/discard endpoints and worker-boundary capture.
Retrying letters re-enqueue onto their RQ queue when the payload carries an
entrypoint, and successful jobs resolve their letters. Direction: add filter
jobs and consolidation jobs.

### 6.4 Heartbeat & health monitoring — **Built (partial)**
`/v1/health`, calibration diagnostics, device heartbeats
(`POST /v1/runtime/heartbeat`, `runtime_heartbeats`), and a runtime status
summary exist. `GET /v1/runtime/health` reports DB, state machine, listener,
queue, ASR/TTS, and dead-letter health, and `workers/runtime_daemon.py` ticks
continuously to expire stale sessions, re-enqueue retrying dead letters, and
keep the runtime observable. Direction: provider latency budgets with alerts.

### 6.5 Action router — **Built (partial)**
Commands become approved actions: searches, fleet tasks, HUD cards,
notifications — each with a permission check and ledger entry. The E.D.I.T.H.
command ledger now records focus, fleet, and recognition commands with actor,
target, payload, status, and result; fleet tasks are device-scoped and
capability-checked through a full lifecycle. The runtime action router
(`approved_actions`) adds a per-action approval matrix with approve/deny/
execute endpoints. Direction: extend the ledger to notifications and future
web/file/code actions.

### 6.6 Notifications & attention budget — **Built (partial)**
Quiet hours, daily alert budget, intervention tiers, focus-mode wake gating
("only interrupt if urgent during focus"), and quiet-hours digest batching
(manual + daemon-scheduled, surfaced through `/v1/runtime/sync`) exist.
Direction: APNs/local notification delivery for digests and voice
interruptions.

### 6.7 Offline capture & sync — **Built (partial)**
Clients queue voice/text/live events offline and sync when back: web workbench,
CLI (`ev queue`/`ev sync`), and iOS `OfflineCaptureQueue` share one idempotent
contract (201 synced, 409 duplicate, 422 quarantined); runtime state converges
through `/v1/runtime/sync`. Direction: background-fetch/watch sync and device
presence-aware queue routing.

---

## 7. Training & personalization

**Domain essence:** EVIE must learn the user, not just retrieve from them.
Training happens in four tracks so nothing blocks: voice enrollment (samples to
voiceprints, minutes not weeks), life-data personalization (retrieval and
importance learning from everything stored), an optional adapter fine-tune that
encodes EVIE's voice and working style from the conversation corpus, and
filter self-improvement from the ledger. The vision is that after months, EVIE
genuinely understands how the user thinks, decides, works, and likes to be
spoken to — always evidence-backed, always consent-gated, always rollback-able.
Success means personalization improves measured outcomes (fewer corrections,
better predictions) without ever leaking or misusing the training data.

### 7.1 Voice enrollment training — **Partial**
Samples → voiceprints + liveness calibration; "training" here is enrollment, not
LLM weight updates. Direction: multi-sample enrollment, versioning, revocation.
Implemented: consent-gated multi-sample enrollment, encrypted/versioned
voiceprints, re-enrollment chains, rollback, revocation, data-subject deletion,
and portable export (`/v1/voice/*`, `/v1/training/*`).

### 7.2 Life-data personalization — **Built**
Importance scoring, patterns, preferences, and self-evaluation already
personalize retrieval. Direction: add recommendation-follow/ignore learning and
per-domain calibration. Implemented: consent-gated, evidence-backed importance
calibration — per-memory-type multipliers derived from logged corrections,
usefulness, and recommendation-follow signals, versioned for rollback, applied
transparently by hybrid retrieval, and fully redactable on request
(`/v1/training/personalization/*`).

### 7.3 Adapter fine-tuning (LoRA) — **Partial**
Train an EVIE adapter on filtered responses + user corrections to encode voice,
style, and working style. Direction: versioned adapters, eval gates, rollback;
requires corpus from the filter ledger. Implemented: consent-gated versioned
adapter registry bound to corpus snapshots, deterministic eval gates
(non-empty corpus, corrections present, no leaked secrets), activate/rollback
and erasure (`/v1/training/adapter/*`). Actual weight training remains
provider-dependent (local LoRA or hosted fine-tune).

### 7.4 Training corpus harvesting — **Built**
Derive versioned snapshots from events, response logs, and ledger with user
consent; exclude `never_send_to_model` content. Direction: build the corpus
pipeline before any fine-tuning. Implemented: consent-gated, versioned corpus
snapshots from rated response logs, filter-ledger final texts, and normal
events — `never_send_to_model`/sensitive content is never included,
credentials are redacted, snapshots are deterministic (content hash),
rollback-able, exportable, and erased by data-subject erasure and retention
sweeps (`/v1/training/corpus/*`).

### 7.5 Filter self-improvement — **Built**
Ledger aggregates (defect precision/recall, over-refinement, correction rate)
recalibrate thresholds monthly, and an explicitly applied recalibration
becomes the live filter policy. Direction: automate monthly application and
reporting. Implemented: consent-gated, versioned recalibration reports from
ledger aggregates (blocks, redactions, repairs, over-refinement) plus user
correction/usefulness signals, deterministic threshold proposals, an apply
endpoint that stores the concrete runtime policy (critic iteration cap,
grounding evidence bar, input-guard severity, persona enforcement, EV Sense
confidence floor), runtime consumption by the filter and EV Sense, rollback
that restores the previously applied policy, history, and erasure
(`/v1/training/filter/*`). Nothing changes at runtime until a report is
explicitly applied, and revocation/deletion returns the system to defaults.

### 7.6 Personalization privacy & consent — **Built**
Every training track requires explicit consent, data deletion, and
exportability. Direction: document per-track consent in the privacy center.
Implemented: per-track `consent_records` with grant/revoke lifecycle and access
logging; voice enrollment, verification, and wake are consent-gated and
revocation cascades to active enrollments.

---

## 8. Live data & sensors

**Domain essence:** Memory is what the user tells EVIE; live data is what EVIE
can observe — with permission. Screen activity, ambient audio, health signals,
and location stream through named channels as immutable live events with privacy
levels, feeding user state, EV Sense, and the context compiler. The vision is a
second, continuous channel of understanding: EVIE knows you are in Xcode, that
your heart rate spiked, that you are at the airport — because your own collectors
said so, under your control. Success means the user runs the collectors, EVIE
uses only permitted slices, and every derived insight can be replayed and rebuilt
from the recorded stream.

### 8.1 Live channels & events — **Built**
`live_channels` + immutable `live_events` with batch ingestion, status, and
privacy levels. `GET /v1/live/stream` provides SSE tailing of newly ingested
events with `access=user|model` privacy slices and replay-on-connect via
`since=`. Direction: WebSocket variant for two-way collector control.

### 8.2 Live context in state — **Built**
`GET /v1/state` includes recent live snippets. Direction: weight live context by
recency and privacy and feed it into the ContextCompiler.

### 8.3 Sensor integrations — **Future**
Screen activity, audio transcripts, HealthKit, location (permissioned). Direction:
implement collectors on each OS; user manages collection.

### 8.4 Live-data rebuildability — **Built**
Live events are immutable with consumed flags; `POST /v1/live/rebuild`
deterministically drops and replays the per-channel derived layer
(`live_derived_state`) from the recorded stream, marking every folded event
consumed. `POST /v1/live/retention` enforces the configured window (dry-run
first; only consumed events past the window, latest and provenance-linked
events always kept). Direction: scheduled rebuild/retention jobs and
real-time streaming (WebSocket/SSE).

---

## 9. Intelligence modules

**Domain essence:** These are EVIE's analytical organs — the difference between
an assistant and a companion. Health radar watches the body's readiness and
anomalies; alert radar keeps watch over what the user cares about; EV Sense
predicts what deserves attention and why now; the pattern engine sees loops and
habits; decision intelligence closes the loop between choices and outcomes; the
user state engine tracks the present; interaction intelligence picks the right
mode and assertiveness; the personality engine keeps EVIE herself; the
relationship model learns what works; self-evaluation keeps everything honest.
The vision is one brain where every organ feeds the others: a pattern feeds a
predictions, a prediction feeds an alert, an alert feeds a challenge, a challenge
feeds the relationship model. Success means the modules together make EVIE
proactively useful, not just reactive.

### 9.1 Health radar — **Built**
Readiness score, z-score anomalies, trends, morning brief. Direction: HealthKit
connector, sleep/stress correlations, and readiness-informed scheduling.

### 9.2 Alert radar — **Built**
Watchlist, scan, priority scoring, dedup, dismiss lifecycle. Direction:
permissioned external sources (calendar, GitHub, RSS) and digest builder.

### 9.3 EV Sense predictive layer — **Built**
Decision loops, patterns, deadlines, health anomalies, guardrails, reorders;
intervention scoring with why-now and outcome tracking. Direction: improve
prediction calibration from outcomes and add next-action cards.

### 9.4 Behavioral pattern engine — **Built**
Research loops, tool churn, repeated questions, with evidence/frequency/confidence.
Direction: add goal-drift and project-abandonment detectors.

### 9.5 Decision intelligence — **Built**
Loops, expected-vs-actual outcomes, auto-lessons, follow-ups. Direction: decision
review scheduling and "decision ledger" UI.

### 9.6 User state engine — **Built**
Activity, project, goal, task, topics, constraints, live context. Direction:
continuous update via live data and focus designations.

### 9.7 Interaction intelligence — **Built**
Modes, intent, urgency, emotion, assertiveness L0–L3, strategy block. Direction:
calibrate with relationship stats and self-evaluation.

### 9.8 Personality engine — **Built**
Versioned profile with assertiveness/challenge ceilings. Direction: connect all
output surfaces (voice, HUD, alerts) to the profile.

### 9.9 Relationship model — **Built**
Interaction stats, corrections, follow rates, challenge acceptance. Direction:
use these to adapt intervention thresholds and tone.

### 9.10 Self-evaluation — **Built**
Response log + aggregates by mode. Direction: feed filter thresholds and
personality updates.

---

## 10. E.D.I.T.H. & advanced modules

**Domain essence:** This is the command layer — E.D.I.T.H.'s tactical
intelligence adapted to a life, not a battlefield. Focus designation locks EVIE
onto what matters; the device fleet turns phones and Macs into a coordinated
network; the ops center is a single dashboard of everything; the recognition log
identifies what the user cares about in their own media; the digital twin is an
explainable model of the user; HUD schemas carry information to glasses, watches,
and widgets; tactical mode briefs before high-stakes moments; the research
assistant and maker companion handle science and making; gear telemetry and
navigation cover the physical world. The vision is a personal operations center:
one conversation, one command surface, total awareness of the user's projects,
devices, and plans — with every "target" being a goal, never a person to harm.

### 10.1 Focus designation — **Built**
Lock EV onto a task/project/person/goal; feeds state and HUD focus overlay.
`GET /v1/focus/suggest` ranks lock-on candidates from user state, pending
alerts, open decisions, recent failures, patterns, and live context; the active
focus is excluded from suggestions. Direction: model-assisted ranking.

### 10.2 Device fleet & tasks — **Built**
Fleet presence, gear, capability-checked task dispatch, and a device-scoped
lifecycle (`requested → accepted → running → completed | failed | cancelled`)
with per-transition command-ledger entries. Direction: real device app to
execute tasks (capture photo, run scan, record audio) and wake arbitration.

### 10.3 Ops center — **Built**
Aggregated dashboard (state, focus, health, alerts, fleet, decisions, patterns).
`GET /v1/hud/ops` pushes the center to HUD as a strict `ev.hud.ops.v1` command
card (focus lock, online devices, pending alerts, open decisions, command
cards). Direction: render on AR surfaces.

### 10.4 Recognition log — **Built**
User-tagged labels over user-owned media/live events, linked to entities.
Direction: on-device vision model suggestions, always user-confirmed.

### 10.5 Digital twin — **Built**
Summary of facts, preferences, goals, patterns, relationship, health, with
per-item provenance (`source_event_ids`, `updated_at`, `version`) linking every
twin claim to its source events and audit trail. Direction: versioned twin
snapshots and "what do you know about me?" time-travel views.

### 10.6 HUD schemas — **Built**
`ev.hud.card.v1`, `briefing.v1`, `focus.v1`, `route.v1`, `alert.v1`, enforced by
a central `HUD_SCHEMAS` registry (`validate_hud`) that every surface output
passes through. `GET /v1/hud/alerts` renders pending alerts as strict HUD cards.
Direction: render on Watch/widget/AR.

### 10.7 Tactical mode — **Built**
Pre-event briefings with risks, options, decision history, plus cached
`ev.hud.quickcard.v1` cards (`POST /v1/tactical/prepare`, `GET
/v1/tactical/quick`) so in-the-moment HUD reads hit the cache path
(FR-TACTICAL-03 < 800 ms). Direction: trigger from calendar/live context.

### 10.8 Person finder — **Built**
Last seen, mentions, relationships over user-owned memory. Direction: sightings
from recognition log and live data.

### 10.9 Research assistant — **Built**
Sessions, notes, sources, conclusions with provenance. Direction: web-search
integration with citations and research reviews.

### 10.10 Maker companion — **Built**
Projects, BOM, print queue, reorder signals. Direction: OctoPrint adapter and
learned build sequences.

### 10.11 Gear telemetry & diagnostics — **Built**
Device snapshots and calibration checks, plus `POST /v1/gear/scan` turning the
latest per-device snapshot into ranked, fingerprint-deduped alerts (battery,
storage, CPU, memory) with quiet-hours suppression for non-urgent tiers.
Direction: backup alerts and scheduled "EV checkup" runs.

### 10.12 Navigation & route briefings — **Built**
Next-commitment leave-by cards. Direction: real maps integration later.

---

## 11. Tools & actions

**Domain essence:** EVIE needs hands, not just a mouth. The tool registry
declares what she can do — search memory, search the timeline, look up a person,
inspect a project, calculate safely, check health or gear, list alerts and
research — and the dispatcher executes those calls with full logging. Tool
selection routes simple intents automatically, and future tools (web search,
files, code, external APIs) will follow the same pattern behind permission
gates. The vision is that EVIE can act on the user's behalf: retrieve, compute,
search, and eventually write and run — always within an explicit permission
matrix, always auditable, always undoable where possible. Success means the user
can ask "what's 14% of 3,500?" or "check my battery" and EVIE uses the right tool
without being asked twice.

### 11.1 Tool registry & dispatch — **Built**
Memory, decisions, timeline, person, project, goals, patterns, calculate, health,
gear, alerts, and research tools with a safe AST calculator. Every tool is a formally
declared capability: explicit input schema (types, bounds, enums, no unknown
arguments), output shape, permission scope, read-only boundary, and undoability
marker. The dispatcher validates arguments, enforces sensitive-tool permission
gates, rejects unknown tools, checks the declared output shape, and writes a
full access-log entry for every invocation. Chat runs a bounded tool loop (max
3 rounds) that executes validated calls through the dispatcher and feeds tool
results back to the provider, with sensitive tools gated per request. Direction:
add web, file, code, and API tools behind the same permission matrix.

### 11.2 Tool selection intelligence — **Built**
Rule-based intent routing covering arithmetic and percentage math, person
lookups, projects, goals, health, gear, alerts/calendar, research, patterns, and
timeline queries. Direction: add model-assisted selection fallback with the same
safety caps.

### 11.3 Permissioned web/search — **Built (v1 adapter)**
`search_web` is the `search` integration adapter (`search.query`, scope
`search:read`) behind the standard adapter framework: provider interface via
integration config, deterministic local mode, per-call permission, and vault
credentials. Direction: citation-aware result parsing and per-call approval UI.

### 11.4 Sandboxed code/file tools — **Built**
`/v1/tools/execute` + `/v1/tools/files/read|write` run inside a sandbox root
(`EV_SANDBOX_ROOT`, default `storage/sandbox`): no shell, minimal environment,
hard timeouts, bounded output, traversal rejection, size caps, owner-trust
gate, and full access-log audit per call. The `execute_command` action spec is
backed by the same executor. Direction: versioned drafts and per-call approval
UI.

### 11.5 Action dispatcher & rollback — **Built**
Write-side actions are formally declared capabilities with payload schemas,
output shapes, permission scopes, approval requirements, read-only boundaries,
and undoability markers (`GET /v1/runtime/action-specs`). The runtime rejects
unknown action types and malformed payloads, routes every action through the
permission matrix, logs route/decide/execute/fail/rollback to the access log,
and rolls back executed undoable actions (`POST /v1/runtime/actions/{id}/rollback`);
routine rollback also transitions the linked action. Direction: add real
web/file/code/API adapters behind the same registry.

---

## 12. Security, privacy & compliance

**Domain essence:** Trust is the product's foundation. Authentication is
multi-layered (master key, device tokens, future biometric unlock); privacy
levels govern every byte, with `never_send_to_model` enforced below the prompt;
encryption protects data at rest and in transit; secret and PII guards run on
both sides of the filter; backups and restore drills protect against loss; and
ethics guardrails (no surveillance of strangers, no manipulation, no dependency
nudging, transparency) are non-negotiables. The vision is a system where the
user's entire life can live in one place with the keys in their hand — the model
only ever sees what they permit, deletion is real, and export is always possible.
Success means the boundary is tested at the payload level, not just promised.

### 12.1 Auth & device tokens — **Built**
Master key + per-device tokens with revocation. Direction: add biometric unlock
and per-device scopes.

### 12.2 Privacy levels & model boundary — **Built**
`never_send_to_model` is enforced below retrieval; tests instrument provider
payloads. Direction: extend boundary tests to voice transcripts and filter critic
payloads.

### 12.3 Encryption — **Built (partial)**
Voiceprints, backups, and integration vault secrets are Fernet-encrypted with
scrypt-derived keys. TLS termination remains deployment-side.

### 12.4 Secret/PII protection — **Built**
`app/security/boundary.py` redacts credentials and blocks
`never_send_to_model` payloads at the model boundary; input/output filters
apply the same rules to transcripts and drafts; `app/security/pii.py`
auto-classifies stored events and live events at ingestion, escalating
credentials/cards/SSNs to `never_send_to_model` and emails/phones to
`sensitive` so model-facing slices exclude them by default.

### 12.5 Backups & restore drill — **Built**
`app/services/backup.py` writes authenticated-encrypted `ev.backup.v1`
bundles with a user-held passphrase; `tests/test_backup.py` covers create,
verify, tamper detection, and wipe → restore → count equivalence.

### 12.6 Ethics guardrails — **Built**
No stranger surveillance, no manipulation, anti-dependency guardrails,
transparency. Direction: publish a privacy/ethics statement in-app.

---

## 13. Clients & UX

**Domain essence:** EVIE must be where the user is: a Mac and web surface today,
iPhone and Watch tomorrow, HUD cards on widgets and future AR. The web/CLI client
is the fastest way to validate memory and the filter; the iOS app adds voice,
camera, share-sheet capture, and background capture; the memory browser makes the
invisible store visible and editable; onboarding turns voice enrollment and
initial memory import into a delightful ritual; HUD renderers turn schemas into
screens. The vision is one EVIE across every surface — same memory, same voice,
same relationship — with offline capture that syncs when the network returns.
Success means a capture on the iPhone appears on the Mac within seconds, and the
user never feels like they opened a different product.

### 13.1 Web/CLI client — **Partial**
CLI package skeleton exists. Direction: full CLI (voice via local ASR, capture,
memory browser) and web dashboard.

### 13.2 iOS/Watch app — **Future**
Voice capture, wake listener, HUD cards, share sheet, offline queue. Direction:
build after backend/voice contracts stabilize.

### 13.3 Memory browser UI — **Future**
Timeline, audit view, corrections, time travel. Direction: web first, then app.

### 13.4 Onboarding & enrollment UX — **Design**
Voice enrollment, initial memory import, trust/privacy setup. Direction:
scripted flow after voice core.

### 13.5 HUD rendering targets — **Future**
Watch complications, widgets, future AR glasses. Direction: schema-driven
renderers.

---

## 14. Ops, evaluation & roadmap

**Domain essence:** This is the engineering backbone that lets the project
survive for years. Deployment means a reproducible self-hosted stack; the
evaluation suite gates every change (API tests, retrieval evals, filter evals,
voice EER tests); observability makes health, calibration, and latency visible;
the API contract keeps 85+ endpoints stable and versioned; cost and latency
budgets stop the filter and the provider from spiraling; the roadmap sequences
everything into phases with exit gates. The vision is a system you can rebuild
from a script, measure honestly, upgrade without fear, and run 24/7 for a decade.
Success means every important change ships with evidence, and the system degrades
gracefully when something fails.

### 14.1 Deployment — **Partial**
Docker Compose (Postgres/pgvector, Redis, MinIO), Tailscale plan, and an
Alembic migration chain (`alembic upgrade head` creates the full schema from
the ORM metadata; `make migrate` runs it). Direction: finish Dockerfile/compose
wiring, TLS, and future schema-evolution migrations.

### 14.2 Evaluation suite — **Partial**
30+ API tests; retrieval/filter/voice evals planned. Direction: add seeded corpus
evals, filter gates, voice EER tests.

### 14.3 Observability — **Partial**
Health + calibration exist. Direction: structured logs, latency budgets, filter
ledger dashboards.

### 14.4 API contract & versioning — **Built**
85 OpenAPI paths, versioned v1. Direction: deprecation policy, idempotency,
SSE event schema versioning.

### 14.5 Cost/latency budgets — **Design**
Provider + filter overhead budgets with degradation. Direction: implement meters
and alerts.

### 14.6 Roadmap sequencing — **Design**
Phase 1: deterministic input/output filter + voice enrollment; Phase 2: wake/ASR/
TTS + runtime; Phase 3: critic + training corpus; Phase 4: adapters, clients, AR.
Direction: gate each phase on its evaluation gates.

---

## 15. Perception & multimodal understanding

**Domain essence:** EVIE needs eyes, not just ears. Vision understanding reads
what the user shows her (photos, screenshots, documents) with user-confirmed
labels; screen awareness derives what the user is doing without sending raw
screens; audio scene understanding knows a meeting from music; location and
presence give context for route briefings; multimodal provider input lets the
brain receive typed media when explicitly permitted. The vision is
E.D.I.T.H.-grade perception with a hard ethical line: EVIE understands the user's
world, never surveils strangers, and never sends raw content without permission.
Success means "what does this photo show?" and "what was I working on?" are
answered from real perception with provenance.

### 15.1 Vision understanding — **Built**
EVIE interprets images/documents it is given over user-owned media:
`POST /v1/vision/analyze` runs a permissioned analysis over an attachment,
prefers on-device derived text (OCR/extraction) over raw media, sends raw
images only with explicit permission and a vision-capable provider, records
every perception with provenance (attachment + source event + provider +
`raw_sent` flag), and writes model-suggested labels to the recognition log as
pending (`source="model"`) until the user confirms them. `POST /v1/chat`
accepts `attachment_id` (with `allow_raw_media`) and answers "what does this
photo show?" from the recorded perception, surfacing it in the context window
and chat provenance with the perception event id. Direction: add on-device OCR
adapters and face-free identity hints.

### 15.2 Screen awareness — **Partial**
The live screen channel yields derived context (active app, document/code summary)
into user state and the ContextCompiler; the model slice never receives raw
screen content. Direction: build OS-level collectors that emit text-level events,
then feed them into user state and the ContextCompiler.

### 15.3 Audio scene understanding — **Built**
Audio live events derive a minimal scene representation (speech/music/noise,
in-call/meeting) with confidence, surfaced as `audio_in_call` EV Sense signals
with live-event provenance. Raw audio and transcripts are never included in the
model-facing slice. Direction: add on-device scene classification behind the
existing channel contract.

### 15.4 Location & presence — **Built**
Location live events derive coarse place + presence context (never exact
coordinates/addresses in model-facing context), surfaced as `location_presence`
EV Sense signals and route-briefing context. Direction: add opt-in on-device
collectors and route-briefing integration.

### 15.5 Multimodal provider input — **Built**
The provider contract carries typed media parts (`ChatMessage.media` with
`MediaPart`), the OpenAI-compatible provider renders image/audio/text content
arrays, and providers advertise `supports_media` so raw transmission is never
attempted on a text-only provider. Same permission and privacy rules apply.
Direction: extend the filter envelope to audit media references on every call.

---

## 16. Identity & trust lifecycle

**Domain essence:** Before EVIE can be trusted with a life, the system must know
exactly who the owner is and how trust escalates. An owner identity record binds
voiceprint, trusted devices, and passkey; trust escalation means casual chat is
lightweight but sensitive actions require re-verification; recovery guarantees a
lost device or changed voice never locks the user out of their own memory; the
multi-user boundary keeps strangers out today and leaves room for a guest mode
later; session security prevents someone else from continuing an unlocked voice
session. The vision is "EVIE is yours alone, and can prove it" — with recovery
that feels like a safety net, not a hurdle. Success means the wrong voice can
never act, and the right user can always get back in.

### 16.1 Owner identity model — **Design**
EVIE needs one authoritative "this is my owner" record: enrolled voiceprint,
trusted devices, passkey, and recovery material — the anchor for all access
decisions. Direction: design an identity table + enrollment ceremony before voice
goes live.

### 16.2 Trust escalation — **Design**
Not every action needs the same trust: casual chat = verified voice; sensitive
actions (deleting memory, spending, external writes) = re-verification via voice
challenge or passkey. Direction: implement a graded permission matrix with
escalation and logging.

### 16.3 Recovery & fallback — **Design**
Lost device, changed voice, or forgotten passkey must not lock the user out of a
lifetime of memory. Direction: recovery codes, re-enrollment flow, and encrypted
identity backup with a restore drill.

### 16.4 Multi-user boundary — **Future**
Single-user first: unknown voices are refused. Optional guest/second-user mode can
come later with per-person voiceprints and isolation. Direction: keep the
single-user invariant, design tables so a second identity is additive later.

### 16.5 Session security — **Design**
Voice sessions need timeouts, silence-lock, and explicit end-of-session so a
roommate can't continue the owner's session. Direction: runtime policy on session
TTL and re-verification.

---

## 17. Legal & biometric compliance

**Domain essence:** Voiceprints are biometric data, and biometric data has legal
teeth (GDPR, Illinois BIPA, EU AI Act). This domain makes the system lawful and
trustworthy by design: biometric handling defines encryption, retention, and
deletion; the consent lifecycle gives the user explicit, revocable consent per
track (voice, training, live data, integrations); data-subject rights cover
voiceprints and training snapshots, not just memories; regional configuration
adapts residency and retention; a transparency center explains exactly what is
stored, trained, and sent. The vision is a personal AI that you can defend in
front of a regulator — because the architecture made compliance structural, not
an afterthought. Success means deletion requests are fully honored everywhere.

### 17.1 Biometric data handling — **Built**
Voiceprints are encrypted at rest (Fernet + scrypt), access-controlled by
consent, retention-limited by regional policy, and deletable on request with
propagation to derived stores and object-store blobs.

### 17.2 Consent lifecycle — **Built**
Per-track `consent_records` (voice enrollment, training corpus, live data,
adapters, filter self-improvement) with grant/revoke timestamps, reasons,
versions, idempotency, and access logging; revocation cascades to enrollments.

### 17.3 Data subject rights — **Built**
Export covers voiceprints, consents, enrollments, and corpus snapshots;
erasure revokes consent, deletes voiceprints + corpus snapshots, tombstones
voice events, redacts derived memories, removes blobs, and writes a purge
manifest for backup/replica handling.

### 17.4 Regional compliance — **Built**
Region, retention windows, residency mode, remote-processing gates, and
disclosures are configuration-driven (`EV_REGION`, `EV_RETENTION_*`,
`EV_RESIDENCY_MODE`, `EV_ALLOW_REMOTE_*`) and enforced at the processing
boundary, erasure, and scheduled sweep (`EV_COMPLIANCE_SWEEP_HOURS`).

### 17.5 Transparency center — **Built**
`GET /v1/compliance/transparency` reports what is stored/trained/processed/
transmitted with retention and deletion paths; the web workbench renders the
report in a Privacy & transparency panel.

---

## 18. Integrations & ecosystem

**Domain essence:** EVIE's knowledge and power grow with what she can reach.
Calendar, health, GitHub, smart home, and messaging integrations arrive through a
standard adapter framework; OAuth tokens live in an encrypted vault with scopes
and revocation; webhooks push external events into live channels so EV Sense
sees them; plugins let the user add custom skills and commands; every integration
carries its own privacy scope. The vision is an ecosystem where EVIE connects to
the user's digital life the way an executive assistant connects to an office —
reading what is permitted, acting only when authorized, and keeping every
credential safe. Success means adding a new integration is a config change, and
revoking it is instant and complete.

### 18.1 Adapter framework — **Built (v1)**
Calendar, health, GitHub, smart home, and messaging adapters implement one
standard interface (capabilities, scopes, privacy floor, event types, actions)
registered in `app/integrations/adapters.py`. Adding an integration is a
registry/config change; provider specifics stay behind the adapter contract.

### 18.2 OAuth & token vault — **Built (v1)**
Third-party tokens and webhook secrets are Fernet-encrypted at rest
(`app/integrations/vault.py`) with per-integration scopes, refresh-token
storage, fingerprints for verification, and immediate revocation that wipes
ciphertext. Plaintext never enters logs, prompts, memory, or model context.

### 18.3 Webhooks & triggers — **Built (v1)**
`POST /v1/integrations/webhook/{id}` verifies HMAC-SHA256 signatures over a
timestamp, rejects replays outside the skew window, rate-limits per
integration, and ingests translated events into the integration's bound live
channel (idempotent, fail-closed privacy) so EV Sense sees them.

### 18.4 Plugin & user extensions — **Built (v1)**
Plugins declare a manifest with explicit permissions and command handlers;
approval is master-key-only, and execution runs in an isolated subprocess with
AST-level sandbox rules (no imports, dunders, filesystem, network, or dangerous
builtins). Declared capabilities gate context and side effects (`live:emit`).

### 18.5 Integration privacy — **Built (v1)**
Every integration owns its privacy scope, bound live channel, and isolated
credentials. Scopes must be a subset of the adapter's declared capabilities,
config cannot carry secrets (vault only), and webhook/plugin events obey the
channel's privacy level — health defaults to `sensitive`.

---

## 19. Routines & automations

**Domain essence:** This is EVIE's proactivity engine — the difference between
an assistant that waits and one that remembers. Scheduled routines run recurring
work (morning brief, weekly review, backups, decision follow-ups); trigger-based
automations fire from state and live data ("deadline 24h out → prepare a brief",
"readiness low → reschedule heavy work"); every automated action is logged,
sensitive ones approved, and undoable where possible; a routine library turns
personal history into reusable templates; observability shows every run and
failure. The vision is an EVIE that quietly handles the predictable parts of a
life so the user only steps in for decisions. Success means automations run
reliably for months, never surprise the user, and can be switched off in one tap.

### 19.1 Scheduled routines — **Built**
`app/routines/service.py` + `POST /v1/routines` run scheduled routines with
timezone, quiet-hours skip, backfill limits, cooldowns, and missed-run
handling.

### 19.2 Trigger-based automations — **Built**
Routines accept trigger conditions over state/live data and fire on matching
events with dedupe keys.

### 19.3 Approval & undo — **Built**
Routine actions route through `ApprovedAction` (approval required when
flagged) and support undo/rollback with a run-level undo status.

### 19.4 Routine library — **Built**
Template library and repeated-failure alerting ship in the routines service;
learned-sequence suggestions remain future work.

### 19.5 Automation observability — **Built**
Run history, attempts, errors, undo state, and failure alerts are exposed via
the routines API and observability overview.

---

## 20. Deliberately excluded candidates (with reasons)

**Domain essence:** Eight candidates were considered as top-level domains and
deliberately kept out to avoid duplicated ownership and scope creep. They are
not unimportant — each is a work item that belongs inside an existing domain as
a sub-factor (schema governance under Memory/Ops, localization under Voice,
design system under Clients, cost and observability under Ops, hardware under
Models/Clients, disaster recovery under Security, business under roadmap). The
vision is disciplined scope: every necessary thing is listed, but the domain
map stays clean enough that each domain has one owner and one clear goal.

These were considered as standalone domains and **excluded** — either because
they belong inside an existing domain (a domain would duplicate ownership) or
because they are not needed for the single-user v1. They are still work items;
they just are not top-level domains.

### 20.1 Data & schema governance — excluded (sub-factor, not domain)
Schema migrations (Alembic), retention windows, validation, and archival are
necessary engineering, but they cut across Memory foundation (§1) and Ops (§14).
Making it a domain would split ownership. Plan: add sub-factors — 1.12 schema
migrations & evolution, 14.7 retention & archival policy.

### 20.2 Multilingual & localization — excluded (low priority for v1)
The system is single-user; the user's own language is the primary requirement,
and ASR/TTS providers handle most language switching. Plan: a sub-factor under
Voice (§5) — 5.8 multi-language ASR/TTS + locale-aware formatting — rather than a
domain.

### 20.3 UI/UX design system & accessibility — excluded (inside Clients & UX)
A design system, voice-first UX patterns, and accessibility are product-critical,
but they are implementation details of §13. Plan: sub-factors 13.6 design system
and 13.7 accessibility & voice-first UX.

### 20.4 Cost & resource governance — excluded (inside Ops)
Provider spend, filter overhead, and per-device power budgets already live in
§14.5 (cost/latency budgets). Plan: expand to 14.8 provider-spend dashboard and
per-device power telemetry, not a new domain.

### 20.5 Observability & telemetry — excluded (distributed across domains)
Runtime health (§6.4), filter ledger (§3.7), and API observability (§14.3)
already cover it. A standalone telemetry domain would fragment ownership. Plan:
add a unified dashboard that reads from all three.

### 20.6 Hardware & edge AI — excluded (roadmap, not v1 domain)
Local 2B models, wearable power budgets, and AR glasses are real directions but
they are constraints on other domains (§4.4 local models, §13.5 HUD targets),
not a domain of their own yet. Plan: revisit when hardware choices are concrete.

### 20.7 Disaster recovery & business continuity — excluded (inside Security/Ops)
Encrypted backups, restore drills, and failover belong to §12.5 and §14.1.
Plan: sub-factor 14.9 restore-drill schedule rather than a domain.

### 20.8 Business/product/marketing — excluded (out of technical scope)
This is a personal self-hosted project; monetization, go-to-market, and
roadmap-as-business are not engineering domains. Plan: keep product thinking in
PLAN.md/ROADMAP.md only.
