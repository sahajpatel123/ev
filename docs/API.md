# EV — API Contract (v1)

**Version 1.0** — the public contract for clients and integrations. Companion to
`ARCHITECTURE.md` §10.

## 1. Conventions

- Base URL: `https://<ev-host>/v1` (Tailscale/Caddy; localhost in dev).
- Auth: `Authorization: Bearer <master-key>` or per-device token after pairing.
- Time: RFC 3339 UTC (`2026-08-09T09:00:00Z`). Clients render local time.
- IDs: UUID v4 strings.
- Pagination: cursor-based (`next_cursor` opaque) or `limit` (max 500).
- Errors: JSON `{"detail": {"code": "...", "message": "...", "fields": {...}}}` with
  standard HTTP status codes.
- Idempotency: clients send `Idempotency-Key` on writes; duplicate keys return the
  original result without re-inserting.
- Versioning: additive changes only within v1; breaking changes require a new major
  version and client capability negotiation.

## 2. Endpoint map

| Method | Path | Purpose | FR |
| --- | --- | --- | --- |
| POST | `/v1/events` | Capture event | FR-MEM-01 |
| GET | `/v1/events/{id}` | Read event | FR-MEM-01 |
| DELETE | `/v1/events/{id}` | Tombstone + redact | FR-SEC-04 |
| GET | `/v1/timeline` | Event timeline | FR-MEM-06 |
| GET | `/v1/recall/week` | Reconstruct a past week (events + end-of-week memory state + consolidation) | FR-MEM-06, FR-MEM-03 |
| POST | `/v1/chat` | Chat (SSE streaming) | FR-ORCH-01..03 |
| GET | `/v1/memories` | Browse memories | FR-MEM-03 |
| GET | `/v1/memories/{id}` | Memory detail | FR-MEM-02 |
| GET | `/v1/people` · `/v1/decisions` · `/v1/goals` · `/v1/preferences` · `/v1/patterns` | Typed browsing | FR-MEM-09 |
| GET | `/v1/audit/{memory_id}` | Why-do-you-know-that | FR-MEM-02, FR-COMP-03 |
| GET | `/v1/conflicts` | Open/resolved conflicts | FR-MEM-07 |
| POST | `/v1/export` | Full export bundle | FR-SEC-04 |
| POST | `/v1/attachments` | Blob capture (multipart) | FR-DEV-01 |
| GET | `/v1/attachments/{id}` | Blob download (auth) | FR-DEV-01 |
| POST | `/v1/vision/analyze` | Permissioned attachment analysis (local OCR → labels → provenance) | FR-D15 |
| GET | `/v1/vision/perceptions` · `/v1/vision/perceptions/{id}` | Perception audit (list/detail, `?attachment_id=` filter) | FR-D15 |
| GET | `/v1/vision/log?source=model\|user` | Recognition log: pending vs user-confirmed labels | FR-D15 |
| POST | `/v1/vision/recognitions/{id}/confirm` | Promote a model-suggested label (idempotent) | FR-D15 |
| POST/GET | `/v1/live/channels` · POST `/v1/live/events` · GET `/v1/live/status` | Permissioned live data channels (screen/audio/location/vision) | FR-D15 |
| GET | `/v1/health` | System + provider health | FR-DIAG-01 |
| POST | `/v1/devices` · GET `/v1/devices` · DELETE `/v1/devices/{id}` | Device pairing/lifecycle | FR-SEC-01 |
| GET | `/v1/gear` | Gear telemetry (M5) | FR-GEAR-01..03 |
| POST | `/v1/gear/scan` | Ranked, deduped gear alerts (battery/storage/CPU/memory) | FR-GEAR-03 |
| GET | `/v1/alerts` · POST `/v1/alerts/{id}/dismiss` | Alerts (M5) | FR-ALERT-02..04 |
| GET | `/v1/research/sessions` · POST `/v1/research/sessions` · POST `/v1/research/sessions/{id}/notes` | Research (M5) | FR-RESEARCH-01..04 |
| GET | `/v1/projects` · POST `/v1/projects` · GET/POST `/v1/projects/{id}/bom` · POST `/v1/projects/{id}/print-jobs` | Maker (M5) | FR-MAKER-01..04 |
| POST | `/v1/focus` · GET `/v1/focus` · POST `/v1/focus/{id}/end` | Focus designation (E.D.I.T.H.) | FR-FOCUS |
| GET | `/v1/focus/suggest` | Ranked focus lock-on suggestions from state/alerts/decisions | FR-FOCUS |
| GET | `/v1/fleet` · POST `/v1/fleet/tasks` · GET `/v1/fleet/tasks[/pending]` | Fleet status + task dispatch/queue | FR-FLEET |
| POST | `/v1/fleet/tasks/{id}/accept` · `/start` · `/complete` · `/fail` · `/cancel` | Device-scoped task lifecycle | FR-FLEET |
| GET | `/v1/commands` · `/v1/commands/{id}` | Auditable E.D.I.T.H. command ledger | FR-SEC-XX |
| GET | `/v1/ops/center` · `/v1/twin` · `/v1/hud/focus` | Ops center / digital twin / HUD overlay | FR-HUD |
| GET | `/v1/twin?as_of={ts}` | Digital twin at a past moment (time travel) | FR-MEM-02 |
| GET | `/v1/hud/card` · `/v1/hud/alerts` · `/v1/hud/route` | Strict HUD surface schemas (`ev.hud.*.v1`) | FR-HUD-01 |
| GET | `/v1/hud/ops` | Unified ops center as HUD command card (`ev.hud.ops.v1`) | FR-HUD-01 |
| POST | `/v1/tactical/prepare` · GET `/v1/tactical/quick` | Tactical quick cards (`ev.hud.quickcard.v1`, cached) | FR-TACTICAL-03 |
| POST | `/v1/gateway/chat` · `/v1/gateway/tools` · GET `/v1/gateway/models` | Internal gateway (model-agnostic) | FR-SYS-03 |
| POST | `/v1/filter/evaluate` | Filter input/output replay (draft or full pipeline) | FR-ORCH-05 |
| GET | `/v1/filter/ledger` · `/v1/filter/ledger/aggregate` | Filter-decision audit trail + aggregates | FR-ORCH-06 |
| GET | `/v1/integrations/catalog` · `/v1/integrations` · POST `/v1/integrations` | Adapter catalog + install | FR-INT-01 |
| GET/PATCH/DELETE | `/v1/integrations/{id}` · `/v1/integrations/{id}/scopes` | Integration lifecycle + scope changes | FR-INT-02 |
| POST/GET | `/v1/integrations/{id}/credentials` · POST `/v1/integrations/{id}/credentials/refresh` · POST `/v1/integrations/{id}/webhook-secret` | Encrypted credential vault + OAuth refresh + webhook secrets | FR-INT-02 |
| POST | `/v1/integrations/vault/rotate` | Re-encrypt all integration credentials under a new vault key (master only) | FR-INT-02 |
| POST/GET | `/v1/integrations/{id}/actions` · `/v1/integrations/{id}/events` | Permissioned actions + event history | FR-INT-03 |
| POST | `/v1/integrations/webhook/{id}` | HMAC-verified webhook ingress (no bearer auth) | FR-INT-04 |
| POST/GET | `/v1/plugins` · `/v1/plugins/{id}` · `/approve` · `/reject` · `/enable` · `/disable` | Plugin manifest lifecycle + approval | FR-INT-05 |
| POST | `/v1/plugins/{id}/commands/{command}` | Sandboxed plugin command execution | FR-INT-05 |

## 3. Event capture

```http
POST /v1/events
Idempotency-Key: <uuid>
Content-Type: application/json

{
  "source": "ios",
  "event_type": "voice",
  "text": "Decided to use SQLite for local testing.",
  "occurred_at": "2026-08-09T09:00:00Z",
  "metadata": {"duration_ms": 4200},
  "device_id": "iphone-16-pro",
  "conversation_id": null,
  "privacy_level": "normal"
}
```

`201 Created` → `EventOut` (includes `id`, `sha256`, `ingested_at`).

Errors: `401` auth, `422` validation, `409` duplicate idempotency key with different
body.

## 4. Timeline

```http
GET /v1/timeline?limit=50&cursor=<occurred_at>&source=ios&event_type=note&since=...&until=...
```

`200` → `{"events": [EventOut], "next_cursor": "..."}`. Tombstoned events are excluded
unless `include_tombstoned=true` (audit-only).

## 5. Chat (streaming)

```http
POST /v1/chat
Content-Type: application/json

{"message": "Why did I decide to use SQLite?", "conversation_id": null,
 "device_id": "macbook", "stream": true, "model": null}
```

SSE event stream:

```text
event: memory-delta
data: {"action":"created","memory_type":"observation","id":"…","text":"…"}

event: provenance
data: {"memory_id":"…","text":"…","score":0.87,"components":{...}}

event: delta
data: {"text":"You decided on 2026-08-09 to use SQLite…"}

event: done
data: {"conversation_id":"…","context_tokens":4210,"model":"deepseek-v4-flash-0731"}
```

Errors mid-stream: `event: error` then `event: done`. Non-streaming returns
`ChatResponse` JSON.

## 6. Memories & typed browsing

```http
GET /v1/memories?memory_type=decision&is_current=true&q=sqlite&limit=20&as_of=2026-08-01T00:00:00Z
```

`200` → `MemoryListResponse` (`memories: [MemoryOut]`, `total`). Filters: `memory_type`,
`is_current`, `q` (hybrid), `as_of`, `privacy_level` (master-key scope), `source_type`,
`redacted` (audit-only).

`GET /v1/memories/{id}` returns full `MemoryOut` with provenance and entities.

## 7. Audit

```http
GET /v1/audit/{memory_id}
```

`200` → `AuditOut`: memory, full version chain, source events, conflicts, access log
entries touching the memory.

## 8. Export & delete

```http
POST /v1/export
```

`200` → `ExportBundle`: events, memories, entities, relationships, conflicts,
attachments metadata, access log. Blobs exported via attachment download endpoints.

```http
DELETE /v1/events/{id}?reason=user-requested
```

`200` → tombstoned `EventOut`. Derived memories redacted; blobs scheduled for deletion
after audit window.

## 9. Attachments

```http
POST /v1/attachments
Content-Type: multipart/form-data
fields: file, event_type=image|file|voice, privacy_level, occurred_at, metadata
```

Creates an event + blob; `201` → `AttachmentOut` + `EventOut`. Download requires
auth and respects privacy levels.

## 10. Devices

```http
POST /v1/devices   {"name":"iphone-16-pro","capabilities":["voice","camera","health"]}
GET  /v1/devices
DELETE /v1/devices/{id}
```

Pairing returns a device token (shown once, stored hashed). Revocation is immediate.

## 11. E.D.I.T.H. command surface

Focus locks EV's attention onto a user-defined goal/task/project/person/topic —
never a person to harm:

```http
POST /v1/focus   {"label":"Ship EV memory engine","kind":"goal","reason":"lock-on"}
GET  /v1/focus
POST /v1/focus/{id}/end
```

Fleet tasks are dispatched to a registered device and must match a declared
capability (or be a universal task: `ping`, `sync`, `report_status`, `ack`).
Lifecycle is `requested → accepted → running → completed | failed | cancelled`,
and every transition is device-scoped: a device token can only read, accept,
start, complete, or fail its own tasks.

```http
POST /v1/fleet/tasks
  {"device_id":"<uuid>","task_type":"capture_photo","payload":{...}}
GET  /v1/fleet/tasks/pending          # device-facing queue (own tasks only)
GET  /v1/fleet/tasks/{id}
POST /v1/fleet/tasks/{id}/accept
POST /v1/fleet/tasks/{id}/start
POST /v1/fleet/tasks/{id}/complete   {"result":{...}}
POST /v1/fleet/tasks/{id}/fail       {"error":"..."}
POST /v1/fleet/tasks/{id}/cancel
```

Every command (focus designation/end, fleet dispatch/transition, recognition
annotation) is appended to the command ledger with its actor, target, request,
status, and result. Master sees all commands; device tokens see only the
commands they issued. `GET /v1/commands` and `GET /v1/commands/{id}` expose the
ledger; `GET /v1/ops/center` includes the five most recent commands.

## 11.5 Perception & vision (Domain 15)

Perception is a permissioned observation layer: analysis only runs with
explicit user permission, respects the source event's privacy level, prefers
on-device derived text (OCR) over raw media, and records provenance for every
conclusion.

```http
POST /v1/vision/analyze
Authorization: Bearer <master-key>
Content-Type: application/json

{
  "attachment_id": "<uuid>",
  "permission": true,
  "allow_raw": false
}
```

Returns a `VisionPerceptionOut` with `summary`, `labels`, `raw_sent`,
`ocr_text`/`ocr_provider`, and provenance ids (`attachment_id`,
`source_event_id`, `perception_event_id` via `GET /v1/vision/perceptions/{id}`).
`permission` must be `true`; raw media is only transmitted when `allow_raw` is
set, the provider advertises vision capability, and the source event is normal
privacy. Sensitive/private/`never_send_to_model` sources are fail-closed.

Suggested labels are recorded with `source="model"` and remain pending until
the user confirms them:

```http
POST /v1/vision/recognitions/{id}/confirm
{"entity_type": "person"}
```

Confirmed labels create a provenance-linked observation memory and appear in
`GET /v1/people/{name}/whereabouts` as sightings.

Live perception channels:

```http
POST /v1/live/events
{
  "channel": "screen-activity",
  "kind": "screen",
  "privacy_level": "sensitive",
  "events": [{"event_type": "focus_change", "payload": {"app": "Xcode", "code_file": "retrieval.py"}}]
}
```

Chat accepts `attachment_id` (and optional `allow_raw_media`) so "what does
this show?" is answered from the recorded perception with provenance.

## 12. Gateway (internal)

- `POST /v1/gateway/chat` — messages + optional tools + request envelope
  (`request_id`, `strategy`, `memories`, `context`) → `GatewayChatResponse`
  with provider, model, latency, and per-call tool-validation outcomes.
- `POST /v1/gateway/tools` — declarative tool dispatch (used by orchestrator);
  accepts `allow_sensitive` and `request_id`, validates arguments against the
  registry schema, enforces the sensitive-tool permission gate, and writes every
  invocation (ok/denied/rejected/error) to the access log.
- `GET /v1/gateway/models` — provider + available models.
- `GET /v1/gateway/calls?limit=&request_id=` — audit view of model calls
  (provider, model, latency, usage, envelope with typed `media_refs`, tool
  validation, errors).
- `GET /v1/gateway/stats?window_hours=` — aggregate latency/error/token
  evidence per provider/model, including `media_refs`, `raw_media_sent`, and
  `derived_media_only` counts for perception audit.

Clients never call the gateway directly; `/v1/chat` is the only model-facing entry
point from the product surface. Every model call through either entry point is
logged to `model_calls` when `EV_MODEL_CALL_LOG_ENABLED=true`.

## 12.5 Intelligence filter

- `POST /v1/filter/evaluate` — body `{message, draft?, conversation_id?}`.
  With `draft`: runs the input filter on `message` and the output filter on
  `draft` against retrieved memory (replay/tuning). Without `draft`: runs the
  full pipeline (input filter → gateway → output filter → ledger). Returns the
  input decision, the output report (draft, final text, claims, scores, flags,
  iterations, passed), and ledger ids.
- `GET /v1/filter/ledger` — filter-decision rows (stage/action/severity/detail/
  envelope hash), newest first.
- `GET /v1/filter/ledger/aggregate` — counts by stage/action, blocked inputs,
  redactions, repairs, refinements, and over-refinement rate.
- `POST /v1/training/filter/recalibration/apply` — apply the current
  ledger-derived recalibration's threshold proposals as the live filter policy
  (critic cap, grounding evidence bar, input-guard severity, persona
  enforcement, EV Sense confidence floor). Reversible via
  `/v1/training/filter/recalibration/rollback`; erasure clears the policy.

Every `/v1/chat` response includes `filter_report`, and streaming chat emits a
`filter-report` SSE event, so clients can show what the filter changed and why.

## 13. Error codes

| Status | Code | Meaning |
| --- | --- | --- |
| 400 | `bad_request` | Malformed request |
| 401 | `unauthorized` | Missing/invalid token |
| 403 | `forbidden` | Valid token, insufficient scope (e.g., revoked device) |
| 404 | `not_found` | Resource absent or tombstoned |
| 409 | `conflict` | Idempotency mismatch / state conflict |
| 422 | `validation_error` | Schema/field errors |
| 429 | `rate_limited` | Client exceeded budget |
| 500 | `internal_error` | Server fault (request id in detail) |
| 503 | `unavailable` | Provider/queue degraded |

## 14. Stability & deprecation

- Endpoints declare `x-deprecated` headers one minor version ahead of removal.
- Clients negotiate capabilities via `GET /v1/health` (`capabilities` field).
- Model/provider selection is per-request (`model` nullable) and never encoded into
  client logic.
