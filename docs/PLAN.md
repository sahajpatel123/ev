# EV — Personal AI Companion

**Plan v3.1 — JARVIS-style lifelong memory, inspired by E.V. (E.V.I.E.) from *Spider-Man: Brand New Day* (2026).**

This plan is the product of record. It supersedes the earlier M0–M4 plan by adding the
full "EV Advanced" layer: every capability E.V. demonstrates in the film is mapped to a
concrete, buildable product module. **No implementation should start until this plan is
approved.**

## 0. Document map

| Document | Contents |
| --- | --- |
| `docs/PLAN.md` | Vision, E.V. component map, principles, persona, architecture summary, modules, API, roadmap, tests, risks, decisions |
| `docs/ARCHITECTURE.md` | System components, tech stack, full data model, pipelines, retrieval algorithm, orchestrator, HUD briefing schema, API examples, configuration |
| `docs/API.md` | Full v1 API contract: endpoints, payloads, SSE streaming, errors, versioning, gateway API |
| `docs/CLIENTS.md` | Client architecture: iOS/Watch modules, web/CLI, offline sync protocol, notifications, HUD rendering targets |
| `docs/REASONING.md` | Prompt architecture, system prompt skeleton, memory formatting, tool schemas, orchestration algorithm, coaching logic, output contracts |
| `docs/MODULES.md` | Deep-dive specs: companion core, health radar, alert radar, EV Sense, tactical mode, maker companion |
| `docs/BEHAVIOR.md` | Behavior upgrade addendum: interaction intelligence, user state, decision/behavior/proactive engines, personality, assertiveness, autonomy guardrails |
| `docs/ROADMAP.md` | Milestone task breakdowns (M0–M5), dependencies, estimates, sequencing, exit gates, suggested calendar |
| `docs/SECURITY.md` | Threat model, trust boundaries, auth, encryption, privacy levels, deletion, backups, model-boundary contract |
| `docs/UX.md` | Persona voice, dialogue patterns, device surfaces, HUD cards, notification design, accessibility, onboarding |
| `docs/REQUIREMENTS.md` | FR traceability: every E.V. component → requirements (FR-SYS…FR-SEC) → milestone → acceptance test |
| `docs/EVALUATION.md` | Test pyramid, invariant suite, retrieval eval, orchestrator tests, companion rubric, module evals, latency budgets |
| `docs/DEPLOYMENT.md` | Self-host requirements, topology, install, TLS, backups, upgrades, failure modes, ops checklist |
| `docs/DECISIONS.md` | Decision log: open decisions with recommended defaults, decided assumptions, change process |
| `docs/DEMO.md` | Scripted milestone demos (M0–M5 + behavior layer) that prove acceptance criteria |
| `docs/GLOSSARY.md` | Shared vocabulary across the suite |
| `docs/QA.md` | Plan QA: objective traceability, automated consistency checks, coverage, assumptions, verdict |
| `docs/EDITH_RESEARCH.md` | E.D.I.T.H. research + E.D.I.T.H.×E.V.I.E. fusion adaptation |
| `docs/CONTINUITY_LIVE.md` | Single continuous conversation + live data recording architecture |
| `docs/INTELLIGENCE_FILTER.md` | Full-duplex intelligence filter architecture v2: voice ID, wake engine, input/output filters, training tracks, 24/7 runtime, 1M-window strategy |
| `docs/WORK_BREAKDOWN.md` | Complete factor catalog: every feature/factor with 3-4 line plans, directions, and status |
| `docs/NEXT_STEPS.md` | Post-breadth productization: dogfood protocol, depth tracks A–E, anti-goals |
| `docs/AGENT_FLEET.md` | Multi-agent SSOT: A0–A9 ownership, merge protocol, bans, done-when gates |
| `docs/AGENT_BRIEFS.md` | Copy-paste work orders (A0–A9) for real presence; Domain 1–19 appendix verification-only |

---

## 1. Vision

EV is a single-user, lifelong personal AI companion that you build and own yourself.
It captures what you experience and tell it, turns that into durable, queryable memory,
and uses DeepSeek V4 Flash (or any future model) as a replaceable reasoning brain on top
of that memory.

**The memory is the product; the model is a replaceable component.**

Like Peter Parker's E.V., ours is:

- **Self-built** — self-hosted, local-first, no vendor lock-in, no dependency on a
  superhero's hand-me-down tech.
- **A companion, not a tool** — it knows your history, notices your state, and speaks to
  you like someone who has been there the whole time.
- **Everywhere at once** — in the "suit" (phone, watch, wearable) and at the "workbench"
  (Mac, web, CLI), sharing one memory.
- **Protective of your secret identity** — your data is yours; the model only ever sees
  what you permit, and deletion is real.

## 2. Inspiration — E.V. in *Spider-Man: Brand New Day*

### 2.1 What the film shows

E.V. (voiced by Naomi Watts) is Peter Parker's self-created AI assistant, built after he
lost access to Stark technology. Reporting describes her as:

- Entirely Peter's own creation — "100% human made" — and the "closest thing Peter has
  to a friend" in his isolated life.
- Built into the suit **and** active at his home workbench, alongside a homemade
  fabricator (an "enhanced" 3D printer).
- A vitals monitor: she performs body scans, tracks his "spider puberty"/arachnid levels,
  and reports readings such as "organic webs, heightened senses, increased agility."
- A diagnostics system for his gear: she monitors web-shooter systems and provides
  analysis, calibrations, and diagnostics.
- A research assistant for his scientific work.
- An alert system that tracks criminal alerts and supplements his natural spider-sense
  with timely real-time alerts.
- A tactical combat analyst: she analyzes scenarios, offers tactical guidance, and
  assists with street navigation through New York.
- A HUD layer: information appears in the eye lenses of the mask, like Karen before her.
- Deliberately **less intrusive** than Karen — present and useful, not nagging.

E.V. also inherits the lineage of Spider-Man's earlier AIs — Karen (suit training,
threat analysis, HUD, diagnostics) and E.D.I.T.H. (AR glasses, drone/tactical network,
surveillance and target designation) — which informs the wearable/AR roadmap.

### 2.2 Component inventory → EV modules

| # | E.V. capability (film) | EV product module | Real-world mechanism |
| --- | --- | --- | --- |
| 1 | Self-built, no Stark tech | **Self-hosted core** | Docker Compose on an always-on Mac; Tailscale; swappable model/STT/TTS providers |
| 2 | Constant companion / only friend | **Companion core** | Persona, relationship memory, tone calibration, adaptive check-ins, coaching |
| 3 | In the suit + at the workbench | **Suit + Workbench surfaces** | iOS/Watch app + Mac/web/CLI on one backend |
| 4 | HUD in mask lenses | **HUD-ready responses** | Structured briefing schemas → Watch complications, Lock Screen widgets, future AR |
| 5 | Vitals / body scans / spider puberty | **Health radar** | HealthKit (HR, HRV, sleep, stress, activity, readiness) + trend/anomaly engine |
| 6 | Web-shooter diagnostics | **Gear telemetry** | Battery/status monitoring of iPhone, Watch, Mac, wearables, backups |
| 7 | Scientific research help | **Research assistant** | Memory-grounded RAG + web/source search with citations |
| 8 | Supplements spider-sense | **EV Sense** | Predictive alert layer from patterns, calendar, health, and deadlines |
| 9 | Criminal-alert tracking | **Alert radar** | Personal topics/projects/people watchlist; calendar/deadline/bill/news alerts |
| 10 | Tactical combat analysis | **Tactical mode** | Pre-event briefings, risk assessment, decision matrices, in-the-moment quick cards |
| 11 | Street navigation | **Navigation assistant** | Route-to-next-event briefings; Apple Maps integration |
| 12 | Diagnostics / calibration | **Self-diagnostics** | System health, queue/db/storage checks, latency monitoring, "EV checkup" |
| 13 | Voice (Naomi Watts) | **Voice layer** | On-device STT + persona TTS; wake word; voice capture |
| 14 | Less intrusive than Karen | **Interaction design** | Attention budget: notification limits, quiet hours, intrusiveness dial |
| 15 | Understands Peter deeply | **Lifelong memory** | Events → facts/decisions/goals/preferences/patterns with provenance + versioning |
| 16 | Protection of identity/privacy | **Secret-keeping** | Local-first data, encryption, privacy levels, tombstone delete, model boundary |

## 3. Product principles (non-negotiables)

1. **Raw events are immutable.** Every input is an append-only event. Deletion is a
   tombstone + redaction; nothing is physically overwritten.
2. **Derived memory is rebuildable.** Facts, summaries, embeddings, graph edges, and
   patterns are derived from events and can be regenerated losslessly.
3. **Memory is versioned, never overwritten.** A changed preference or decision creates
   v2 linked to v1 with a reason-for-change; history is queryable ("what was I thinking
   six months ago?").
4. **The model never holds your life.** EV interrogates memory through retrieval and
   tool calls within a bounded context budget. `never_send_to_model` content is
   physically excluded at the retrieval boundary.
5. **Provenance on everything.** Every answer can answer "why do you know that?" with
   source events, dates, confidence, and change history.
6. **Contradictions are surfaced, not papered over.** Conflicting memories create
   conflict records and clarifying questions.
7. **The user owns the keys.** Self-hosted, exportable, encrypted, auditable, deletable.
8. **A companion, not a manipulator.** No dark patterns, no fabricated intimacy, no
   dependence loops; EV states when it is guessing and lets you inspect any behavior.
9. **Proactive, but never intrusive.** EV earns attention; notifications respect quiet
   hours and an explicit attention budget.

## 4. Companion core (persona spec)

### 4.1 Persona

- **Name:** EV (E.V.I.E. — "Everything Vitals & Intelligence, Engineered" or as the user
  prefers; naming is configurable).
- **Temperament:** warm, precise, dry wit; calm under pressure; honest about uncertainty.
  Thinks in your vocabulary, not corporate AI speak.
- **Memory behavior:** never forgets what you asked it to remember; references dates and
  sources when it uses memory; asks before inferring anything sensitive.
- **Coaching voice:** inform → recommend → challenge, escalating only with evidence:
  - **L1 Inform:** facts, summaries, reminders, briefings.
  - **L2 Recommend:** options with tradeoffs, drawn from your decision history.
  - **L3 Challenge:** when it detects repeated loops — e.g., ≥3 similar re-evaluations in
    30 days → "you've re-evaluated this N times — what would make this decision final?"
    Always cites the previous decisions and their outcomes.
- **Boundaries:** transparently AI; does not impersonate a human; does not invent
  memories; can be asked "why do you know that?" at any time.

### 4.2 Relationship memory

- Interaction history (frequency, topics, tone, times of day).
- Tone calibration: matches your energy and preferred directness (configurable).
- Mood/state inference from text, timing, and (with permission) health signals —
  used only for supportive behavior, never for marketing-style targeting.
- Adaptive check-ins: more frequent during busy/high-stress periods, minimal during
  quiet hours; the cadence itself is learnable and inspectable.

## 5. System architecture

```text
Devices (iOS, Watch, Mac/web, CLI, future AR/wearable)
        ↓  explicit, user-permitted data only
Ingestion API (FastAPI) → immutable event → background queue
        ↓
Memory Engine (PostgreSQL + pgvector + Redis + S3-compatible storage)
        ↓
Memory Orchestrator (understand → retrieve → rank → assemble → write)
        ↓
AI Gateway (DeepSeek V4 Flash primary; provider-swappable)
        ↓
Response: answer / advice / action / reminder / challenge / briefing
```

### 5.1 Deployment

- Self-hosted on an always-on Mac: Docker Compose (API, Postgres/pgvector, Redis, MinIO,
  workers).
- Tailscale for phone access; TLS in transit; encryption at rest; single-user master key
  + device registration.
- Offline-capable clients: capture queues locally and syncs when back online.
- Everything runs locally by default; cloud providers (model APIs) receive only
  permitted, assembled context.

## 6. Data & memory model

**Status: Built** — implemented and verified (2026-08-10); tracked per-factor in
[`docs/WORK_BREAKDOWN.md`](WORK_BREAKDOWN.md) §1.

### 6.1 Stores

| Store | Purpose | Rebuildable from events? |
| --- | --- | --- |
| `events` | Raw immutable input (text, voice, image, file, note, share, conversation) | — (source of truth) |
| `memories` | Typed derived memory: episodic, semantic, fact, decision, goal, preference, observation, pattern, summary; version chain stored in-row (`version_group`, `version`, `supersedes`, `valid_from`/`valid_until`, reason) | Yes |
| `entities` | People, places, projects, topics, organizations | Yes |
| `entity_relationships` | Typed, weighted, time-valid edges | Yes |
| `conflicts` | Contradiction records (open/resolved) | Yes |
| `health_snapshots` | Vitals/readiness series (HR, HRV, sleep, stress, activity) | No (external source) |
| `gear_telemetry` | Device/watch battery, backups, hardware status | No (external source) |
| `alerts` / `notifications` | Delivered, dismissed, snoozed alert history | Partially |
| `research_sessions` | Research questions, sources, notes, conclusions | Yes |
| `projects` / `bom_items` / `print_jobs` | Maker projects, materials inventory, fabricator queue | Partially |
| `attachments` | Blobs in S3-compatible storage | Yes (blob + metadata) |
| `access_log` | Every read/write/export/delete | Yes (append-only) |
| `devices` | Registered devices + capabilities | Yes |

### 6.2 Event shape

```text
event_id, occurred_at, ingested_at, source, event_type,
content, metadata, device_id, conversation_id, privacy_level
```

`privacy_level ∈ private | normal | sensitive | never_send_to_model` is enforced at the
retrieval boundary: the orchestrator physically cannot include `never_send_to_model`
content in model context.

### 6.3 Memory invariants

- Every memory traces to ≥1 source event (`memory_events` provenance).
- `source_type ∈ explicit | inferred | derived`; inferred claims are phrased as
  observations, never facts.
- Temporal fields (`event_time`, `created_time`, `updated_time`, `valid_from`,
  `valid_until`) make time-travel queries first-class.
- Tombstoning an event redacts every memory derived from it; the rows remain for audit.
- Export includes events + derived state; import/restore is a full round-trip.

## 7. Intelligence stack

### 7.1 Hybrid retrieval (locked default)

```text
FinalScore = 0.35·Semantic + 0.20·Keyword + 0.15·Recency
           + 0.15·Importance + 0.10·Relationship + 0.05·Confidence
```

- Semantic via dedicated embedding model (pgvector); keyword via token overlap; recency
  via exponential decay; importance from extraction scoring; relationship from entity
  overlap; confidence from source strength.
- Per-component scores are returned with every result for transparency and debugging.
- Hierarchical descent: entity/topic indexes first; detailed events only when the
  question demands it.

### 7.2 Context budget & memory tools

- Normal chat assembles a bounded context (default ~20k tokens).
- The large model window is a scratch workspace for progressive reasoning, never a
  life-dump.
- Memory tools exposed to the model (bounded, terminating loop, max 3 rounds):
  - `search_memory(query, k, memory_type?)`
  - `search_decisions(query, k)`
  - `search_timeline(query, k)`
  - `get_behavior_patterns(topic?, k)`
  - `get_health_trends(metric?, window_days?)`
  - `get_gear_status(device?)`
  - `get_upcoming_events(window_days?)`
  - `search_research(query, k)`
- Every tool result includes date, type, score, and provenance ids; results are capped
  and trimmed to budget.

### 7.3 Coaching / challenge engine

- Pattern engine detects repeated behaviors (frequency, first/latest observed,
  confidence).
- Decision-loop detector: same topic re-evaluated N times in a window → L2/L3 response
  with citations to prior decisions and outcomes.
- Contradiction surfacing: open conflicts are included in context and responses ask
  which version is current.

## 8. Feature modules (specifications)

### 8.1 Capture & memory (M0–M2) — **Built**

- Capture: text, voice, image, file, share-sheet, notes, conversations.
- Every capture → immutable event → extraction → typed memories → embeddings →
  entities/relationships → conflict/version checks.
- Memory browser: timeline, people, decisions, goals, preferences, patterns, audit
  trail; everything is versioned and redactable.

### 8.2 Health radar (M5) — "vitals & spider puberty"

- **Sources (permission-gated):** Apple Health/HealthKit via iOS/Watch: heart rate,
  HRV, resting HR, sleep stages, steps, workouts, active/resting energy; manual mood
  check-ins.
- **Derived signals:** daily readiness score, sleep debt, stress trend, activity
  balance, anomaly detection (e.g., HRV drop, sleep regression, unusual late-night
  activity).
- **Behavior:** morning readiness brief; anomaly alerts; trend summaries ("your sleep
  has improved 3 weeks running"); contextual check-ins before/after intense days.
- **Privacy:** health data stays in the health store with `sensitive` privacy level;
  never sent to the model unless explicitly permitted; exportable; deletable.

### 8.3 Gear telemetry (M5) — "web-shooter diagnostics"

- Monitors the user's gear: iPhone/Watch/Mac battery, storage, backup health,
  wearable charge; later: e-bike, headphones, smart devices.
- Alerts and pre-departure checklists ("Watch at 12% — charge before you leave").
- Diagnostics/calibration: EV's own model/embedding/STT/TTS providers health.

### 8.4 EV Sense (M5) — "supplements your spider-sense"

- Predictive layer combining patterns, calendar, health, deadlines, and alert history
  to anticipate needs before being asked:
  - "Tomorrow's meeting with X — here's the decision history."
  - "You usually crash after 1am nights; your readiness is already low."
  - "Renewal due Friday; you've forgotten it two years running."
- Ranking by predicted usefulness; quiet hours; explicit intrusiveness dial; every
  prediction is inspectable ("why did EV tell me this?").

### 8.5 Alert radar (M5) — "criminal alerts"

- **Watchlist:** projects, topics, people, companies, products the user tracks.
- **Sources (permission-gated):** calendar, reminders, deadlines, bills, email/RSS
  mentions, GitHub/repo events, price changes.
- **Behavior:** dedup, priority routing, digest batching, quiet hours, notification
  budget; "neighborhood watch" for your life, not a firehose.

### 8.6 Tactical mode (M5) — "combat analysis"

- Triggered by high-stakes events (interviews, negotiations, deadlines, first days) or
  an explicit "tactical" command.
- **Pre-event briefing (JSON/HUD-ready):** objective, context, people, risks, options
  with pros/cons, decision history, recommended action, talking points, open questions.
- **In-the-moment quick cards (< 800 ms):** precomputed context + retrieval, rendered as
  Watch complication / Lock Screen card / voice one-liner.
- Risk assessment grounded in past outcomes from memory; latency budget: pre-event
  briefing < 3 s, quick card < 800 ms.

### 8.7 Research assistant (M5) — "scientific research"

- Memory-grounded RAG: your past notes/decisions + web/source search.
- Research sessions: question, working notes, sources (saved as events/attachments),
  hypotheses, conclusions; citations on every answer.
- "Remember this finding" → durable memory with provenance.

### 8.8 Navigation assistant (M5) — "street navigation"

- Route-to-next-event briefings; travel time estimates; "leave by" alerts; location
  context for notes ("at the gym" → workout note).
- Apple Maps integration; HUD-ready ETA cards.

### 8.9 Maker companion / fabricator (M5) — "homemade fabricator"

- Projects: design files, BOM, build logs, step-by-step instructions.
- Materials inventory: quantity, location, reorder thresholds, cost tracking.
- Print queue: OctoPrint/3D-printer integration, job status, failure detection alerts.
- "EV, print the next part" → guides the full workflow.

### 8.10 Workbench (M2+) — "home base"

- Mac desktop/web/CLI surface: dashboard (today, alerts, health, gear, projects),
  capture, memory browser/editor, research sessions, system health.

### 8.11 Suit (M2+) — mobile/wearable

- SwiftUI iOS app: voice, camera, share sheet, notes, AI chat, memory browser,
  timeline; Watch app for quick capture and HUD cards; offline capture queue + sync.

### 8.12 Voice & HUD (M5)

- Voice capture (on-device STT), persona TTS voice, wake word.
- HUD-ready response schema (cards, complications, widgets); AR glasses later
  (E.D.I.T.H.-style overlay) — interfaces designed now, hardware deferred.

### 8.13 Self-diagnostics (M0+)

- System health: DB, queues, storage, gateway latency, extraction/embedding error
  rates; "EV checkup" endpoint; diagnostics log; calibration events.

## 9. Devices & UX flows

| Flow | Devices | Latency target |
| --- | --- | --- |
| Capture "remember this" | iOS, Watch, Mac, CLI | < 1 s ack |
| Chat / question | iOS, Mac, web, CLI | first token < 1.5 s |
| Memory browse/audit | Mac, web, iOS | < 500 ms |
| Tactical briefing | Watch, iOS, Mac | < 3 s pre-event; < 800 ms quick card |
| Health brief | Watch, iOS | morning digest |
| Alert | iOS/Watch notifications | near-real-time, batched |
| Voice capture | iOS, Watch | on-device STT |

Multi-device consistency: one backend, event-sourced; clients sync offline queues;
conflict-free because all state derives from append-only events.

## 10. Security & privacy ("secret identity")

- Local-first data; TLS in transit; encryption at rest; key management; encrypted
  backups with restore drills.
- Single-user auth: master key + registered devices; device revocation.
- Privacy levels enforced at retrieval; `never_send_to_model` boundary tested at the
  prompt-assembly seam.
- Full export (JSON bundle) and delete (tombstone + redaction) endpoints.
- Audit + access logs on every read/write/export/delete.
- Threat model: model APIs are untrusted storage; only permitted, assembled context
  leaves the machine; DeepSeek never sees raw memory.

## 11. Public API surface (v1)

- `POST /v1/events` — capture text/voice/image/file/note
- `POST /v1/chat` (streaming) — response + memory deltas + retrieval provenance
- `GET /v1/timeline | /v1/memories | /v1/people | /v1/decisions | /v1/goals |
  /v1/preferences | /v1/patterns`
- `GET /v1/audit/{memory_id}` — "why do you know that?"
- `POST /v1/export`, `DELETE /v1/events/{id}` — portability + tombstone
- `POST /v1/attachments`
- `GET /v1/health`, `GET /v1/gear`, `GET /v1/alerts` (M5)
- `GET /v1/research/sessions`, `GET /v1/projects`, `GET /v1/print-jobs` (M5)
- Internal gateway: `/chat`, `/reason`, `/stream`, `/tools`, `/models`

## 12. Roadmap

### M0 — Skeleton (backend + minimal app)

**Deliverables:** Docker Compose stack (FastAPI, PostgreSQL/pgvector, Redis, MinIO,
gateway→DeepSeek); immutable event ingestion; chat with basic retrieval; memory
browser; self-diagnostics; CLI + minimal web.

**Acceptance:** "remember this" → later query returns the memory with its source; events
are immutable (tombstone only); full suite green.

### M1 — Memory core

**Deliverables:** extraction engine, embeddings, entities/relationships,
decisions/goals/preferences, timeline, versioning, contradiction detection,
audit/export/delete, access log.

**Acceptance:** "why did I decide X?" answers with provenance; changed preference
produces v2 with intact v1; contradictions produce conflict records; derived data
regenerates from events.

### M2 — The app (product surface)

**Deliverables:** SwiftUI iOS app (voice, camera, share sheet, notes, AI chat, memory
browser, timeline) + Watch quick capture; Mac/web/CLI on same backend; multi-device
sync; offline capture queues.

**Acceptance:** capture from either iPhone or Mac appears on all devices within
seconds; offline captures sync on reconnect.

### M3 — Intelligence

**Deliverables:** memory tool-calling, hierarchical retrieval, context monitor,
behavioral pattern engine, coaching levels (inform → recommend → challenge),
proactive notifications.

**Acceptance:** repeated research loop triggers Level-3 challenge citing previous
decisions/outcomes; context usage stays within budget; tool results bounded.

### M4 — Hardening

**Deliverables:** security review, encrypted backups + restore drill, at-rest
encryption, key management, performance, long-horizon consolidation ("how has my
thinking changed since…?").

**Acceptance:** backup restore verified; export/delete verified; latency budgets met.

### M5 — EV Advanced

**Deliverables:** companion core (persona, tone, adaptive check-ins, coaching engine);
health radar; gear telemetry; EV Sense; alert radar; navigation; research assistant;
tactical mode (briefing schema + quick cards); maker companion (projects, BOM, print
queue); voice & HUD-ready schemas; self-diagnostics; Watch/AR-ready interfaces.

**Acceptance:** each module has a demonstrable vertical slice with tests; a "meeting
with X" query produces a tactical briefing with decision history, risk assessment, and
provenance; health/gear anomalies generate ranked, quiet-hours-respecting alerts;
interfaces validate against HUD-ready JSON schema.

## 13. Verification & testing

### Memory invariants — **Built (verified)**
- No code path updates or deletes raw events (tombstone only).
- Dropping all derived data (embeddings/summaries/patterns) and regenerating from
  events yields equivalent state.
- Every memory traces to ≥1 event; versioning preserves v1 after changes;
  contradictions create conflict records.

### Retrieval quality
- Seeded corpus; representative queries ("which coding model should I use?", "who did I
  meet last month who liked AI?", "what did I decide about X and why?") surface the
  intended memory in top-5 ≥80% of cases.
- Temporal queries respect dates; dedup prevents repeated identical memories.

### Orchestrator behavior
- Tool calls return bounded results; progressive retrieval terminates; assembled
  context never exceeds budget; `never_send_to_model` verifiably absent from every
  prompt (tested at the boundary).

### API & app flows
- Capture → chat → timeline → audit → export → delete round-trip; every read/write in
  access log; auth rejects unauthorized devices; multi-device sync consistency;
  offline capture queues.

### EV Advanced
- Health/gear ingest → trend/anomaly outputs tested on synthetic data.
- Alert precision/recall on synthetic watchlist corpus; notification budget enforced.
- Tactical briefing schema validated (JSON schema + latency budgets).
- Companion safety tests: no fabricated memories, no manipulative patterns, boundary
  prompts handled ("you're just an AI" → honest, non-defensive).

### Security
- TLS, at-rest encryption, key management, backup restore drill, verified
  export/delete.

## 14. Risks & mitigations

| Risk | Mitigation |
| --- | --- |
| Model API dependency (DeepSeek) | Gateway abstraction from day one; echo/mock providers; local model option later |
| Health data sensitivity | On-device storage flags, `sensitive`/`never_send_to_model`, export/delete, encryption |
| Notification fatigue / intrusiveness | Attention budget, quiet hours, digest batching, intrusiveness dial; measured |
| Scope creep in EV Advanced | Every module gets a vertical slice + acceptance criteria; sequencing in M5 |
| iOS background limits | Offline capture queues, background fetch, Watch for quick capture |
| Retrieval quality drift | Locked scoring formula + eval harness; per-component transparency |
| Single-user scale vs cloud features | Local-first by design; optional cloud only for model calls |
| Voice latency | On-device STT; async pipeline; quick-card path precomputed |

## 15. Open decisions (need user input)

1. **Sequencing:** build M0+M1 backend first, or start with an EV-Advanced vertical
   slice (e.g., tactical mode + health radar) on a minimal backend?
   *Recommended default:* M0 → M1 → M2 → M3 → M4, then M5 slices (companion core →
   health radar → alert radar → tactical → research → navigation → maker → voice/HUD →
   gear → EV Sense). Rationale in `ROADMAP.md` §2.
2. **Health data scope:** full HealthKit (HR/HRV/sleep/stress) vs activity-only for v1
   of the health radar?
   *Recommended default:* read-only HR/HRV/sleep/activity from HealthKit, all stored
   locally as `sensitive`; anomalies gated behind explicit opt-in.
3. **Web research backend:** which search/source API (permission + cost)?
   *Recommended default:* start with an OpenAI-compatible embedding API and a
   user-supplied search key (Brave/SerpAPI) behind a provider interface; no key = no
   web search, memory-only research still works.
4. **Voice stack:** on-device Whisper + local TTS vs hosted STT/TTS (privacy vs quality)?
   *Recommended default:* on-device Whisper for capture; provider-interface TTS with a
   local fallback; hosted voices only with explicit consent.
5. **AR/wearable:** design HUD schemas now, hardware later — confirmed?
   *Recommended default:* yes — HUD-ready JSON schema in M5, hardware deferred.
6. **Model:** DeepSeek V4 Flash 0731 default — confirm; keep gateway swappable?
   *Recommended default:* yes, keep gateway abstraction; echo/mock for offline dev.
7. **Branding:** name it EV/E.V.I.E. or configurable persona name?
   *Recommended default:* "EV" as product name, configurable persona name/voice.

Until these are answered, the recommended defaults above are the plan's working
assumptions; any decision can be changed later without architecture rework.

## 16. Deferred / out of scope (v1)

- OS-level ambient capture (notifications, WhatsApp, background mic) — only where the
  OS genuinely permits explicit, user-approved capture.
- Multi-user/shared memory; organizations; cloud SaaS.
- Drone integration (E.D.I.T.H.-style) — deferred with AR/wearable roadmap.
- Real-time city/alert networks — represented by the personal alert radar instead.

## 17. Stretch directions (post-M5, not committed)

These are the "more and more advanced" horizons that extend the E.V. inspiration
without being promised:

| Direction | Inspiration | Sketch |
| --- | --- | --- |
| Local brain | Peter builds everything himself | Ollama/llama.cpp as a gateway provider; on-device embeddings; full no-cloud mode |
| Autonomous actions (permissioned) | E.D.I.T.H. drone operations | EV proposes + executes only pre-authorized actions: send summary, file bug, order reorder item, start print — each with an approval log |
| Multi-modal memory | Body scans / suit sensors | Photo understanding, audio moments, screen captures → typed memories with embeddings |
| Digital-twin consolidation | E.V. knows Peter's whole arc | Long-horizon "state of me" reports: thinking changes, relationships, goals, patterns over years |
| Second-brain integrations | Workbench | Read/write bridges to notes (Obsidian/Notion), code repos, calendar, email — permissioned adapters |
| Community skills/plugins | "100% human made" ethos | User-contributed module packs (alert sources, maker printers, HUD targets) via a signed-plugin system |
| AR glasses / HUD hardware | Mask-lens HUD | Render `ev.hud.*` schemas on real hardware; camera context capture |
| Ambient sensing (consented) | Spider-sense | User-approved home sensors, location, screen-time → richer EV Sense, always revocable |
| Companion voice/motion | Naomi Watts-level presence | Local neural TTS with persona emotion; animated companion on Watch/Mac |

Each stretch direction reuses the same memory engine, gateway, and HUD schema, so none
require architecture rework — only new modules.

## 18. Current workspace state (historical context — implementation has moved past this)

The repository already contained a non-code foundation before implementation; that
scaffold has since been built out into the M0–M5 breadth tracked in
[`docs/WORK_BREAKDOWN.md`](WORK_BREAKDOWN.md) (statuses per factor). The bullets below
are historical:

- Git repo initialized; `.env.example`, `compose.yaml`, `Makefile`.
- `backend/pyproject.toml`; package skeleton (`app/`, `tests/`, `clients/`).
- Domain models (`events`, `memories`, `entities`, `conflicts`, `access_log`,
  `devices`, `attachments`), API schemas, provider contracts, config.
- Memory-core services (extraction, hybrid retrieval, versioned writer, pattern
  engine, orchestrator scaffolding), gateway providers (echo/mock/DeepSeek),
  embeddings (hash/http), auth, event service, object storage, processor.

That scaffold seeded M0; per-factor implementation status now lives in
`docs/WORK_BREAKDOWN.md`.

## 19. Behavior & interaction upgrade (addendum summary)

`docs/BEHAVIOR.md` adds the next evolution: EV as a persistent intelligence layer
around a replaceable model. Highlights:

- **Interaction Intelligence Layer** between reasoning and response: deterministic
  mode selection (casual / technical / analytical / coaching / emergency /
  collaborative), tone from a structured personality profile, and adaptive response
  length (minimum useful communication).
- **User State Engine**: continuously updated current activity, project, goal, task,
  focus, recent successes/failures — so EV never re-derives "what are we working
  on?" from scratch.
- **Decision intelligence**: extended decision schema (context, problem, options,
  reason, expected/actual outcome, related goal/project) with a follow-up loop that
  writes lessons from outcomes.
- **Behavioral intelligence**: evidence-backed patterns (research loops, tool churn,
  abandonment, repeated mistakes, goal drift) with confidence and provenance.
- **Constructive challenge**: assertiveness L0–L4, evidence-gated; L4 requires
  explicit standing permission; never shaming.
- **Proactive intelligence**: intervention score (importance × urgency × confidence
  × goal relevance × benefit) → do nothing / mention later / notify; predictive
  assistance with "why now?" rationale.
- **Tool orchestration**: memory + web/file/code/API tools with selection
  intelligence and sandboxed, permissioned execution.
- **Personality engine** (structured, versioned, consistent core), **relationship
  model** (evidence-backed), **self-evaluation** and **prediction tracking**.
- **Per-action permission matrix** (access/store/send-to-model/send-to-service/act)
  enforced at every engine boundary.
- Critical review included: Level-4 interventions refined, autonomy (P9) deferred,
  emotional inference consent-gated, "scolding" rejected in favor of evidence-based
  challenge.

P1–P8 map into M3/M5 (see `ROADMAP.md`); P9 is post-M5. Requirements are
`FR-BHV-01…23` in `REQUIREMENTS.md`.

---

**Plan status:** v3.1 — implementation status is tracked per factor in
`docs/WORK_BREAKDOWN.md`; the memory & data foundation (Domain 1) is verified
**Built** (2026-08-10). Next step: continue per `docs/NEXT_STEPS.md`.
