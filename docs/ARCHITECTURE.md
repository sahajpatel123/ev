# EV — Architecture Plan

**Version 1.0** — companion document to `PLAN.md`. Specifies components, data model,
pipelines, retrieval, orchestration, HUD schemas, API examples, and configuration.

## 1. System overview

```text
┌─────────────────────────── Clients ───────────────────────────┐
│ iOS (SwiftUI) · Watch · Mac desktop/web · CLI · future AR      │
└───────────────┬────────────────────────────────────────────────┘
                │ HTTPS + SSE (Tailscale / LAN)
┌───────────────▼────────────────────────────────────────────────┐
│ API (FastAPI, async)                                           │
│  /v1/events /v1/chat /v1/memories /v1/audit /v1/export ...      │
└───────────────┬────────────────────────────────────────────────┘
                │ immutable event
┌───────────────▼────────────────────────────────────────────────┐
│ Processor (sync inline or Redis/RQ queue)                      │
│  extract → embed → dedup → version → conflict → entities       │
└───────────────┬────────────────────────────────────────────────┘
┌───────────────▼────────────────────────────────────────────────┐
│ Memory Engine                                                  │
│  PostgreSQL + pgvector (source of truth)                       │
│  Redis (queues/cache) · MinIO/S3 (attachments)                 │
└───────────────┬────────────────────────────────────────────────┘
┌───────────────▼────────────────────────────────────────────────┐
│ Orchestrator                                                   │
│  decompose → retrieve (hybrid) → assemble context → tool loop  │
└───────────────┬────────────────────────────────────────────────┘
┌───────────────▼────────────────────────────────────────────────┐
│ AI Gateway (provider registry)                                 │
│  DeepSeek V4 Flash (default) · echo/mock (offline)             │
│  Embeddings: dedicated model (hash fallback for dev)           │
└────────────────────────────────────────────────────────────────┘
```

## 2. Tech stack

| Layer | Choice | Why |
| --- | --- | --- |
| API | FastAPI (async, Python 3.12+) | Typed schemas, SSE streaming, single language |
| DB | PostgreSQL 17 + pgvector | One source of truth; vector search; JSONB |
| Queue | Redis + RQ | Simple durable jobs; already in compose |
| Objects | S3-compatible (MinIO local) | Attachments; portable to any S3 |
| Embeddings | Dedicated embedding API (OpenAI-compatible) | Plan requires embeddings outside the chat model |
| Chat | DeepSeek V4 Flash 0731 via gateway | Default; echo/mock providers for offline dev |
| Clients | SwiftUI iOS/Watch, Mac web/desktop, CLI | One backend, many surfaces |
| Deployment | Docker Compose on always-on Mac + Tailscale | Local-first; phone access; DIY ethos |

## 3. Repository layout

```text
ev/
  docs/                 # plan suite (PLAN, ARCHITECTURE, ROADMAP, SECURITY, UX)
  infra/                # compose overlays, Tailscale notes, backups
  backend/
    app/
      api/              # HTTP routers
      gateway/          # chat providers + registry
      memory/           # extraction, retrieval, writer, patterns, orchestrator
      services/         # events, processor, export/delete, health, alerts, research
      storage/          # object store abstraction
      workers/          # RQ jobs
      models.py         # SQLAlchemy ORM
      schemas.py        # Pydantic API schemas
      config.py         # env-driven settings
    alembic/            # migrations
    tests/
  clients/
    ios/                # SwiftUI app (future)
    cli/                # ev capture / chat / timeline
    web/                # minimal browser client served by FastAPI
```

## 4. Data model

### 4.1 `events` — raw, immutable

`id uuid PK`, `occurred_at timestamptz`, `ingested_at timestamptz`,
`source varchar(32)`, `event_type varchar(64)`, `content jsonb`, `metadata jsonb`,
`device_id varchar(128)`, `conversation_id uuid`, `privacy_level varchar(32)`,
`sha256 varchar(64)`, `tombstoned_at timestamptz NULL`, `tombstone_reason text NULL`.

No UPDATE/DELETE paths exist in code. `sha256` covers content+metadata+source+type so
any tampering is detectable in audit.

### 4.2 `memories` — derived, versioned

`id uuid PK`, `memory_type varchar(32)`, `text text`, `payload jsonb`,
`importance float`, `confidence float`, `source_type varchar(16)`,
`privacy_level varchar(32)`, `event_time timestamptz`, `created_time`,
`updated_time`, `valid_from timestamptz`, `valid_until timestamptz NULL`,
`version_group uuid`, `version int`, `supersedes_id uuid NULL`,
`superseded_by_id uuid NULL`, `reason_for_change text NULL`, `is_current bool`,
`redacted bool`, `fingerprint varchar(64)`, `embedding vector(384) NULL`, `extra jsonb`.

Indexes: `(memory_type, is_current)`, `(valid_from, valid_until)`, `fingerprint`,
`privacy_level`, `source_type`, HNSW on `embedding` (production Postgres only).

### 4.3 Associations & graph

- `memory_events(memory_id, event_id)` — provenance, many-to-many.
- `memory_entities(memory_id, entity_id, role, weight)`.
- `entities(id, entity_type, name, aliases jsonb, summary, canonical_key unique)`.
- `entity_relationships(id, from_entity_id, to_entity_id, relationship_type,
  weight, valid_from, valid_until, source_type, source_event_id, created_time)`.

### 4.4 Conflict & audit

- `conflicts(id, memory_id_a, memory_id_b, reason, status open|resolved|dismissed,
  resolution, resolution_memory_id, created_time, resolved_time)`.
- `access_log(id, occurred_at, actor, action, endpoint, resource_type,
  resource_ids jsonb, request_id, details jsonb)` — append-only writes.
- `model_calls(id, request_id, actor, provider, model, status, latency_ms,
  prompt_tokens, completion_tokens, tool_calls jsonb, envelope jsonb,
  envelope_hash, error, created_at)` — append-only audit of every gateway model
  call; envelope holds strategy, memory refs, request id, and metadata, and
  `envelope_hash` correlates each call with its filter-ledger decisions.

### 4.5 EV-Advanced stores (M5)

- `health_snapshots(id, recorded_at, source, metrics jsonb, privacy_level)` —
  metrics: `hr, hrv, resting_hr, sleep_minutes, sleep_stages, steps, workouts,
  stress, readiness, energy`.
- `gear_telemetry(id, recorded_at, device, battery_percent, storage_free,
  status, details jsonb)`.
- `alerts(id, created_at, kind, title, body, priority, status, source jsonb,
  triggered_by_event_id, dismissed_at)`.
- `research_sessions(id, title, question, status, created_time, updated_time)` +
  `research_sources(id, session_id, url, title, snippet, captured_event_id,
  fetched_at)` + `research_notes(id, session_id, note, memory_id)`.
- `projects(id, name, status, created_time, updated_time)` +
  `bom_items(id, project_id, name, qty, unit, location, reorder_at)` +
  `print_jobs(id, project_id, filename, status, started_at, finished_at,
  printer, error)`.
- `attachments(id, event_id, filename, content_type, size_bytes, storage_key,
  sha256, created_at)`.
- `devices(id, name, token_hash, created_at, last_seen_at, revoked_at,
  capabilities jsonb)`.

## 5. Ingestion pipeline

1. `POST /v1/events` validates and hashes the payload.
2. Event is inserted (immutable); response returns `event_id`.
3. Processor dispatch:
   - `EV_PROCESSING_MODE=sync` (dev/tests): inline `process_event`.
   - `queue` (compose/prod): enqueue RQ job `app.workers.jobs.process_event`.
4. `process_event`:
   - Write episodic memory for the event.
   - Run rule extraction (Section 6) → typed candidates.
   - Dedup by fingerprint; version on semantic-key change; detect conflicts.
   - Embed canonical text; link entities; persist provenance.
5. Nightly/weekly consolidation jobs: daily summaries, pattern recompute,
   health/alert scans, research session summaries. All non-destructive.

## 6. Extraction rules (v1)

Rule-based extractor over event text (LLM-assisted extraction is an optional later
upgrade behind the same `MemoryCandidate` interface):

| Pattern (examples) | Memory type | source_type |
| --- | --- | --- |
| "I decided to/that/on X" | decision | explicit |
| "I prefer/like/love/hate X (over Y)" | preference | explicit |
| "I want to / need to / plan to X" · "my goal is X" | goal | explicit |
| "my name/job/city/... is X" · "I live/work at X" | fact | explicit |
| Any meaningful statement not matching above | observation ("Observed: ...") | inferred |
| Any captured event | episodic | explicit |
| Repeated topic engagement (≥3 in window) | pattern | derived |
| Daily/weekly/monthly consolidation | summary | derived |

Inferred claims are phrased as observations with lower confidence; they never enter
the fact store as facts.

## 7. Retrieval

### 7.1 Candidate selection

`is_current = true`, `redacted = false`, privacy filter by access mode, optional
`memory_type`, optional `as_of` (validity window).

### 7.2 Scoring (locked default)

```text
FinalScore = 0.35·Semantic + 0.20·Keyword + 0.15·Recency
           + 0.15·Importance + 0.10·Relationship + 0.05·Confidence
```

- Semantic: cosine over embeddings (pgvector or in-process fallback).
- Keyword: Jaccard over normalized tokens.
- Recency: `exp(-days_since_event/90)`.
- Importance: extraction score (0–1).
- Relationship: max overlapping entity-link weight for query entities.
- Confidence: stored confidence (0–1).

Every result returns `components {semantic, keyword, recency, importance,
relationship, confidence}` for transparency and eval.

### 7.3 Event timeline search

Keyword+recency over raw events (tombstones excluded, privacy filtered) — used by
`search_timeline` and the timeline API.

## 8. Orchestrator & context assembly

### 8.1 Chat flow

1. Ingest user message as an immutable event (`message.user`).
2. Process the event (memories + embeddings + conflicts).
3. Retrieve: main memories, decisions, preferences, goals, patterns; open conflicts
   involving retrieved items; broaden once if signal is thin.
4. Assemble context within `EV_CONTEXT_BUDGET_TOKENS` (~20k default):
   - System persona prompt.
   - Sections: Relevant memory · Decisions · Preferences · Goals · Patterns ·
     Conflicts (with dates, types, source types, confidence).
   - Lowest-score items are trimmed first; provenance stays visible.
5. Tool loop (max 3 rounds): `search_memory`, `search_decisions`,
   `search_timeline`, `get_behavior_patterns`, `get_health_trends`,
   `get_gear_status`, `get_upcoming_events`, `search_research`.
6. Gateway response → assistant event (`message.assistant`).
7. Return reply + `memory_delta` + `provenance` + `context_tokens`.

### 8.2 Privacy boundary

`access="model"` retrieval excludes `privacy_level = never_send_to_model` at the SQL
level; a dedicated test asserts no such content appears in any assembled prompt.
`private`/`sensitive` items are allowed only with explicit per-item opt-in.

## 9. HUD-ready briefing schema (M5)

```json
{
  "schema": "ev.hud.briefing.v1",
  "objective": "Negotiate the contract renewal with X",
  "context": [
    {"type": "memory", "id": "…", "text": "Decided: prefer fixed-term contracts", "date": "2026-07-14"}
  ],
  "people": [{"name": "X", "relationship": "client", "notes": "…"}],
  "risks": [
    {"risk": "Underbid on scope", "likelihood": "medium", "impact": "high",
     "mitigation": "Quote from past project costs"}
  ],
  "options": [
    {"option": "Fixed + milestone payments", "pros": ["…"], "cons": ["…"],
     "past_evidence": [{"memory_id": "…", "outcome": "worked well in March"}]}
  ],
  "recommendation": "…",
  "talking_points": ["…"],
  "open_questions": ["…"],
  "latency_ms": 0
}
```

Same shape renders as Watch complication, Lock Screen widget, or AR overlay.

## 10. API examples

### Capture

```http
POST /v1/events
Authorization: Bearer <master-key>
Content-Type: application/json

{
  "source": "ios",
  "event_type": "note",
  "text": "I decided to move the project to SQLite for local testing.",
  "privacy_level": "normal",
  "device_id": "iphone-16-pro"
}
```

### Chat

```http
POST /v1/chat

{"message": "Why did I decide to use SQLite?", "device_id": "macbook"}
```

```json
{
  "reply": "You decided on 2026-08-09 to move the project to SQLite for local testing…",
  "conversation_id": "…",
  "model": "deepseek-v4-flash-0731",
  "context_tokens": 4210,
  "memory_delta": [{"id": "…", "memory_type": "observation", "action": "created", "text": "…"}],
  "provenance": [
    {"memory_id": "…", "text": "Decided: …", "memory_type": "decision",
     "score": 0.87, "components": {"semantic": 0.9, "keyword": 0.4, "…": 0.0}}
  ]
}
```

### Audit

```http
GET /v1/audit/{memory_id}
```

Returns the memory, full version chain, source events, conflicts, and access log —
the complete "why do you know that?" answer.

### Delete (tombstone)

```http
DELETE /v1/events/{id}?reason=user-requested
```

Marks `tombstoned_at`, redacts derived memories, keeps rows for audit.

### Export

`POST /v1/export` → JSON bundle: events, memories, versions, entities, relationships,
conflicts, attachments metadata, access log.

## 11. Worker jobs & schedules

| Job | Queue | Schedule | Idempotency |
| --- | --- | --- | --- |
| `process_event` | ingestion | on event | provenance dedup |
| `embed_batch` | embedding | on event / nightly | skip existing |
| `recompute_patterns` | patterns | nightly | version-update on change |
| `consolidate_summaries` | consolidation | nightly/weekly | period fingerprint |
| `scan_health` | health | hourly (M5) | snapshot dedup |
| `scan_alerts` | alerts | every 15 min (M5) | alert fingerprint |
| `scan_gear` | gear | hourly (M5) | per-device latest |
| `backup` | ops | daily | snapshot id |

## 12. Configuration (env)

| Variable | Default | Purpose |
| --- | --- | --- |
| `EV_DATABASE_URL` | sqlite dev / postgres prod | DB |
| `EV_REDIS_URL` | localhost:6379 | queue |
| `EV_MASTER_KEY` | dev-only | auth |
| `EV_PROCESSING_MODE` | sync | sync vs queue |
| `EV_EMBEDDING_PROVIDER` | hash | hash vs http |
| `EV_EMBEDDING_BASE_URL/KEY/MODEL/DIM` | — | embedding API |
| `EV_CHAT_PROVIDER` | echo | echo/mock/deepseek |
| `EV_DEEPSEEK_BASE_URL/API_KEY/MODEL` | — | chat API |
| `EV_CONTEXT_BUDGET_TOKENS` | 20000 | context cap |
| `EV_MAX_RETRIEVAL_MEMORIES` | 50 | candidate cap |
| `EV_OBJECT_STORE_BACKEND` | local | local vs s3 |
| `EV_STORAGE_ROOT` | ./storage | local blob root |
| `EV_S3_*` | — | MinIO/S3 |
| `EV_ACCESS_LOG_ENABLED` | true | audit |

## 13. Scale & performance notes

- Single-user: expected tens of thousands of events; in-process cosine is acceptable,
  pgvector HNSW is available in production.
- Retrieval candidates capped at `EV_MAX_RETRIEVAL_MEMORIES·4` before scoring.
- Context assembly is O(candidates); trimming is score-ordered.
- Client latency budgets: chat first token < 1.5 s; tactical quick card < 800 ms
  (precomputed context); timeline/memory browse < 500 ms.

## 14. Edge cases & constraints

| Case | Handling |
| --- | --- |
| Clock skew | Server clamps `occurred_at` > now to `ingested_at`; original kept in metadata; cursors use server order. |
| Duplicate delivery | `Idempotency-Key` on writes; content `sha256` detects same-payload duplicates. |
| Re-capture after tombstone | New event with new id; old audit trail intact. |
| Privacy level change | New version of the memory with new level; never mutate history. |
| Very large inputs | Text truncated to configured cap and stored as attachment; pipeline processes metadata only. |
| Corrupt/partial events | Validation rejects; failed extraction retried; events never lost (queue + dead-letter). |
| Multi-language text | Tokenization is Unicode-aware; embeddings provider-neutral; keyword scoring falls back to semantic. |
| Timezone | All storage UTC; clients render local; "day" boundaries defined by user timezone in settings. |
| Concurrent capture | Append-only events; single-writer processing; dedup by idempotency key. |
| Model outage | Requests queue or return explicit fallback; memory read/write unaffected. |
| Health data gaps | Snapshot dedup by (recorded_at, source); gap detection alerts rather than imputation. |
| HUD content overflow | Cards truncate by tier (compact/medium/full); full briefing always linked. |
| Emoji/formatting | Stored as-is; canonical text used for embedding; rendering strips control chars. |

## 15. Behavior & interaction layer (addendum)

See `BEHAVIOR.md` for the full spec. Architecture delta:

```text
...DeepSeek Reasoning
        ↓
Interaction Intelligence (state → mode → tone → strategy → assertiveness)
        ↓
Response
```

New stores:

| Store | Purpose |
| --- | --- |
| `user_state` | Single-row current state (activity, project, goal, task, focus, recent topics/successes/failures) |
| `session_state` | Ephemeral working context per conversation; expires on inactivity |
| `decision_outcomes` | Expected vs actual outcome links for decisions |
| `predictions` | Prediction text, confidence, basis ids, outcome, reviewed_at |
| `interventions` | Proactive events: score components, policy result, delivery/response |
| `response_log` | Mode, tone, assertiveness, length, tokens, model, latency, outcome feedback |
| `relationship_stats` | Evidence-backed counts (successful/failed recs, corrections, preferences) |
| `personality_profile` | Structured, versioned profile params + core invariants |

These stores are derived/operational: `user_state` and `session_state` rebuild from
events; `response_log`/`predictions`/`interventions` are append-only operational
records. All respect the per-action permission matrix (§21 of `BEHAVIOR.md`).
