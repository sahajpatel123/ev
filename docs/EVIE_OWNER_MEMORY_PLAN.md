# Evie Owner Memory + Fetching — Implementation Plan

Source: ONLY prior results 1–8 (compact evidence). No other feature scope.
Scope: owner memory + fetching ONLY. Single-owner, local-first, offline-green.

## 0. Implementation status (2026-10-08, owner-directed execution)

All six phases executed on the working tree (uncommitted, reviewable):

- Phase 0: `owner_fact_recall`, `owner_provenance_answer`,
  `owner_state_no_moralize` gates live in `backend/app/scripts/eval_gates.py`,
  registered in `_run_all`, SKIP-first (12 tests).
- Phase 1: 5 tables + migration `u7v8w9x0y1z2`, 4 `/v1/owner/*` endpoints,
  `EV_OWNER_MODEL_ENABLED` / `EV_OWNER_STATE_TTL_S`, contract +4 paths.
- Phase 2: owner-fact extraction, `owner_kind` routing, `EV_OWNER_DISTILL_MODE`
  (default shadow), SECRET/ASSISTANT laws enforced on the distill path.
- Phase 3: retrieval owner-boost hook (formula untouched), bounded owner
  context block wired into the filter pipeline, `EV_OWNER_RETRIEVAL_BOOST_MAX`
  / `EV_OWNER_CONTEXT_TOKENS`. `personalization.py` intentionally unedited
  (bounds + consent track reused, no learning-behavior change).
- Phase 4: memory chips cite source events, owner-row chips, owner grounding
  material, `GET /v1/owner/model/{row_id}/audit`, `EV_OWNER_CHIPS_ENABLED`.
- Phase 5: affect snapshots (`EV_OWNER_STATE_ENABLED`,
  `EV_OWNER_STATE_CONSENT_TRACK`), no-moralize guidance echo in live + chat
  paths, `GET /v1/everywhere/owner/changes` + bootstrap `owner_model` key.
  The shared `VISIBLE_*` event stream was deliberately NOT extended.

Still not real after implementation: measured `eval/ml/owner_*.json`
artifacts (gates SKIP honestly without them); Swift consumption of
`/v1/everywhere/owner/changes` (`ios/EVClient/Sources/EVClient/` and
`macos/Sources/EV/` call `/v1/everywhere/*` but not the sync endpoints yet —
SUIT follow-up).

## 0b. Every-turn follow-up (2026-10-09, owner-directed execution)

Audit finding: neither LIVE compiler included the owner block — the Phase 3
block only reached the `/v1/filter` pipeline, while real turns use
`/v1/chat` → `ContextCompiler` and devices/voice → kernel
`compile_context`. Fixed both: fetch `owner_context_for_prompt` in
`chat()` and `_mimo_turn`, thread through, capped sections. Chat still
answers if the fetch fails. Added `app/scripts/owner_backfill.py`
(dry-run default, consent-gated `--apply`, resumable) for history.
Verified live on a scratch server: migrate → consent → talk → rows in
`/v1/owner/model` → rows in `/v1/everywhere/owner/changes`, chat 200.
Production cutover deliberately NOT executed (deployment law): flags +
migration + backfill steps handed to the owner (see session report).

## 1. Problem (ghost → partner)

Evie is a ghost today: it stores events and derived memories with provenance
and rebuild (`backend/app/services/rebuild.py:141-150`), retrieves them with a
locked hybrid score (`backend/app/memory/retrieval.py:23-34`), and guards the
model boundary (`backend/app/security/boundary.py:100-152`) — but it has no
owner model (traits/values/thinking-style/state), no lifecycle that turns
capture into applied guidance, and no provenance-bearing answers grounded in
who the owner is. A partner remembers what matters, fetches it at the right
moment with receipts, adapts to state without moralizing, and syncs across the
owner's devices via the home station.

## 2. Goals / non-goals

Goals (memory + fetching only):

- Owner model store: traits / values / thinking-style / state, versioned with
  provenance, correctable and forgettable without deleting raw events.
- Closed lifecycle: capture → distill → retrieve → apply → correct, each step
  seam-typed and rollback-safe.
- Retrieval ranking stays locked-formula; owner relevance enters only via
  consent-gated multipliers and weight-multiplier overrides, never silent
  reweighting.
- Every memory-backed answer carries provenance (memory ids + source event
  ids); unsupported claims are removed, not hedged.
- State-aware guidance: adapt tone/pacing/brevity to detected state; never
  moralize, diagnose, or persist affect as fact without consent + label.
- Multi-device fetch via home-station sync (bootstrap + bounded deltas),
  owner-scoped, privacy-tiered.

Non-goals: no new product domain (Domain 20 banned); no multi-user/guest; no
voice/vision/live-data engine work; no weight training before corpus; no SaaS
exposure; no editable log; no public API ports; no changes to frozen
personality sliders.

## 3. Compat bounds (DO-NOT-TOUCH)

From prior results 2 + 7 (fleet law sampled, survives):

- `backend/app/ev/personality.py` — owner-frozen, off-limits to every agent.
- `backend/pyproject.toml`, `backend/uv.lock` — Agent 2 only; DEP REQUEST +
  lazy guarded import otherwise.
- `backend/eval/contract_v1.json` — additive-only, never hand-edit; Agent 1
  regenerates via `make update-contract`. Measured 542 paths / 587 ops
  (2026-10-03); FLEET_LAW's "261 operations" is STALE, do not quote.
- Migrations: `down_revision` = head at start; never edit another agent's
  migration; `CREATE EXTENSION vector` is Postgres-only, SQLite stays clean.
- OWNS splits: Agent 8 owns `embeddings.py`, `memory/retrieval.py`,
  `rerank.py`, `eval/retrieval/**` (MUST NOT touch
  extraction/entities/writer/gateway). Agent 9 owns
  `memory/{extraction,entities,importance,patterns,writer,curator,llm_extractor}.py`,
  `services/{processor,consolidation,recall,rebuild,importer,event_service}.py`
  (MUST NOT touch retrieval/embeddings/voice/filter). Cross-owns work needs a
  DEPENDENCY NOTE naming the agent number.
- Shared files append-only inside `# --- AGENT <N> <CODENAME> ---`:
  `Makefile`, `.env.example`, `compose.yaml`, `docs/ENVIRONMENT.md`,
  `app/{config,models,schemas}.py`, `app/api/{core,ev,edith,companion,tools}.py`.
  Never change an endpoint signature, column, or default.
- ModelArbiter registration required (name/license/source_url/sha256/tiers);
  never load outside arbiter; never download without checksum.
- Reasoning via hosted API; local models only for wake/OCR/speaker/face;
  every remote path passes `remote_processing_allowed()`.
- Offline CI sacred: doubles set `degraded=true`; weight tests
  `pytest.mark.skipif`, skip never fail; ≥1 test hits the real factory entry.
- No commit/push/rebase/stash/revert unless the human orders it.

## 4. Current-state map (file:line)

Store shape (immutable events, versioned memories):

| File:line | What exists |
|---|---|
| `backend/app/models.py:41-70` | `events` table, immutable, `stream_seq` server-owned unique |
| `backend/app/models.py:88-106` | `before_insert` assigns `stream_seq` via counter row |
| `backend/app/models.py:122-146` | `memories` table: `version_group`, `version`, `supersedes_id`, `is_current`, `fingerprint` |
| `backend/app/models.py:172-208` | `memory_events` provenance join; `MemoryEntity`; `EntityRelationship.source_event_id` |
| `backend/app/models.py:1497-1520` | `MemoryCurationJob` outbox (job_key, kind/status/priority/attempts) |
| `backend/app/services/event_service.py:22-64` | `EventService.create`: PII classify, privacy escalate, canonical hash, idempotency hash |
| `backend/app/services/event_service.py:68-80` | `message.*` only enqueue outbox + JSONL journal |
| `backend/app/services/event_service.py:92-111` | `tombstone()` only; refuses double; no update/delete path |
| `backend/app/services/processor.py:36-73` | `process_event_sync`: Extractor → filter_candidates → MemoryWriter |
| `backend/app/services/processor.py:97-102` | `ensure_processed`: `processing_mode` sync\|queue (`config.py:72`) + Redis/RQ probe |
| `backend/app/memory/writer.py:52-115` | same-fingerprint dedupe; changed value → new version + `Conflict` row |
| `backend/app/memory/writer.py:169-186` | every write gets `MemoryEvent` provenance row, deduped |
| `backend/app/memory/state.py:99-123` | `valid_as_of` / `memories_as_of` temporal reconstruction |
| `backend/app/services/rebuild.py:141-150` | rebuild wipes derived layer, replays same Extractor + Writer |
| `backend/app/ev/memory_ops.py:27-98` | correction (explicit v+1, conf 1.0), forget (hide), restore (reverse) |
| `backend/app/memory/extraction.py:51-120` | REAL deterministic regex engine (decision/preference/goal/fact/...) |
| `backend/app/memory/llm_extractor.py:1-10` | LLM extraction optional async enrichment only; hot path never network |
| `backend/app/memory/curator.py:1-5` | MiMo curator background proposer; Writer commits |
| `backend/app/contracts.py:40-51` | `MemoryCandidate` extraction→writer handoff dataclass |
| `backend/app/memory/candidates.py:16-19` | scoring gate `EV_MEMORY_SCORING_V2` off\|shadow\|on |
| `backend/app/schemas.py:93-116` | `MemoryOut` full version/provenance surface |

Retrieval + fetch:

| File:line | What exists |
|---|---|
| `backend/app/memory/retrieval.py:23-34` | SCORE_WEIGHTS: semantic .35 / keyword .20 / recency .15 / importance .15 / relationship .10 / confidence .05 |
| `backend/app/memory/retrieval.py:212-250` | `Retriever.search(...)`; `weight_overrides` are renormalized multipliers |
| `backend/app/memory/retrieval.py:264-280` | candidates: current, non-redacted, non-archive, importance-ordered, cap 4x; model-access privacy exclusion |
| `backend/app/memory/retrieval.py:335-347` | embedding model-version law: NULL=legacy hash; mismatch zeroes semantic |
| `backend/app/memory/retrieval.py:455-480` | rerank post-pass only when k ≤ 10; degraded keeps base order |
| `backend/app/embeddings.py:570-605` | `get_embedder()` factory; `hash` default; http/granite/qwen3 |
| `backend/app/embeddings.py:602-603` | UNKNOWN provider silently falls to hash (contradicts "raise" norm) |
| `backend/app/config.py:149-171` | `EV_EMBEDDING_*`, `EV_RERANKER_*`, `EV_SEMANTIC_NORMALIZE=true` |
| `backend/app/rerank.py:108-127,255-301` | `should_rerank` thresholds; not-ready → base order + `degraded=True` |
| `backend/app/memory/index.py:40-89` | Postgres FTS + ILIKE fallback; sqlite 800-row substring scan |
| `backend/app/db.py:128-131` | pgvector extension PG-only; no ANN index — Python-side cosine scan |

Filter / identity / boundary / state:

| File:line | What exists |
|---|---|
| `backend/app/security/boundary.py:100-175` | `guard_model_payload` raises on forbidden markers, redacts secrets |
| `backend/app/security/pii.py:39-40,80-89` | NEVER_SEND vs SENSITIVE sets; `escalate_privacy` only raises |
| `backend/app/filter/input_filter.py:159-197` | `resolve_privacy_level`; `MemoryBroker` strips never_send at seam |
| `backend/app/filter/pipeline.py:63-165` | full filter pipeline; blocked inputs never reach provider |
| `backend/app/filter/output_filter.py:375-428,1128-1150` | grounding audit per claim; stage order incl. provenance chips |
| `backend/app/ev/user_state.py:29-58,166-250` | access-tiered state; regex-mined failures/successes/constraints; keyword activity |
| `backend/app/schemas.py:2035-2047` | `UserStateOut`: NO mood/affect field |
| `backend/app/ev/interaction.py:37-114,339-355` | regex emotion detect + per-emotion reply policy |
| `backend/app/identity/service.py:34-89,151-166` | trust ladder; OWNER/REVERIFY actions; single-owner anchor |
| `backend/app/models.py:244-261` | `OwnerIdentity`: only `display_name` — NO traits/values fields |
| `backend/app/models.py:2074-2119` | `AssistantProfile` singleton — nearest existing owner-prefs home |
| `backend/app/ev/personality.py:110-148,197-208` | FROZEN 9-slider personality; owner-origins-only mutation rule |
| `backend/app/models.py:1752-1772` | `PersonalizationCalibration`: versioned type→multiplier (0.8–1.2), consent-gated |
| `backend/app/training/personalization.py:28-157` | MIN_EVIDENCE=3; `{}` unless consent active |

Clients / ingest / sync:

| File:line | What exists |
|---|---|
| `backend/app/api/core.py:580-619` | POST /v1/events thin router; 409 on idempotency replay |
| `backend/app/everywhere/sync.py:36-139` | VISIBLE prefixes; owner-trusted sensitive gate; v2 cursor epoch\|stream_seq |
| `backend/app/everywhere/continuity.py:28-45` | bounded resume_context; live offer fails open |
| `backend/app/everywhere/offline_queue.py:30-71` | ≥8-char idempotency key; 409 replay; queued never executed |
| `backend/app/device_gateway/api.py:861` | trusted-device text entry via TurnGate/Evie Core |
| `backend/app/everywhere/owner.py:36` | single CANONICAL_OWNER; sandbox stays isolated |

Contract / eval:

| File:line | What exists |
|---|---|
| `backend/eval/contract_v1.json` | 542 paths / 587 ops locked (2026-10-03) |
| `backend/app/scripts/eval_gates.py:217-276` | api_contract: missing locked endpoint FAILS; unlocked /v1 route FAILS |
| `backend/app/scripts/eval_gates.py:382-477` | retrieval gate in-process (top5, privacy, weights) |
| `backend/app/scripts/eval_gates.py:2088-2154` | retrieval_quality: nDCG@10≥0.80 + top5≥90%, else FAIL; missing/degraded → SKIP |
| `backend/app/scripts/eval_gates.py:1675-1816` | continuity gate: 9 checks, offer + turn ledger reach prompt |
| `backend/app/scripts/eval_gates.py:1261-1328` | regression gate: ML tol 0.01; latency 10%+25ms; rank drift >1 |
| `backend/eval/last-run.json` | 19/21 passed, 150/152 checks, 2 skipped; latency + regression FAIL (tree RED) |
| `backend/tests/test_memory_foundation.py:1-747` | F0+F1 fetching contract: intent taxonomy, labeled envelopes, modes, exactly-once, stale-bleed block, L1 p95<250ms |
| `backend/tests/test_continuity.py:1-54` | self-contained vs continuation classifier contract |

## 5. Target architecture

Owner model (traits / values / thinking-style / state):

- New `owner_model` rows are derived + versioned like memories (version_group,
  supersedes chain, `is_current`), each linked to source events via a
  provenance join. Traits/values/thinking-style are long-lived curated rows;
  state (activity/focus/energy/affect-label) is a short-lived snapshot with a
  TTL, never version-chained, never used as training fact.
- `OwnerIdentity` (display_name) and `AssistantProfile` (nickname/prefs) stay
  read-only seams; the owner model never writes into them. FROZEN personality
  sliders are read for tone, never mutated (mutation rule already rejects
  non-owner origins).
- All owner-model writes require owner trust; affect/state inference rows are
  consent-gated and labeled (per D-12): no silent affect persistence.

Memory lifecycle (capture → distill → retrieve → apply → correct):

- Capture: unchanged `EventService.create` append path; owner-utterance events
  flow through the existing outbox + journal.
- Distill: existing Extractor stays the floor; owner-fact candidates (trait /
  value / preference-statement) enter through `MemoryCandidate` + scoring gate;
  LLM extractor remains optional enrichment behind its interface (DC-04).
- Retrieve: locked SCORE_WEIGHTS formula; owner relevance via
  consent-gated `PersonalizationCalibration` multipliers (0.8–1.2) and
  per-query multipliers; embedding model-version law unchanged.
- Apply: `compile_context_block` seam gains a bounded owner-model block
  (traits/values + current state snapshot) under the existing token budget;
  output filter's provenance-chip stage cites memory + event ids.
- Correct: `memory_ops` correction/forget/restore semantics extend to owner
  rows (explicit v+1 at conf 1.0; forget hides; restore reverses; raw events
  preserved). Rebuild replays owner-model derivation deterministically.

Retrieval ranking + provenance in answers:

- No formula change. Owner boost is multiplicative and capped, applied only
  with active `life_data_personalization` consent and MIN_EVIDENCE met;
  otherwise multipliers return `{}` (current behavior).
- Answers cite `[memory:<id> ← event:<id>]` chips from the existing
  `memory_events` join; grounding audit removes unsupported claims (existing
  stage order preserved).

State-aware guidance without moralizing:

- State detection reuses the existing keyword/regex seams (`user_state`,
  `interaction.detect_emotion`) as the offline floor; any classifier upgrade
  is a provider behind an interface with a degraded path.
- Policy table (acknowledge once, do the task) extends to owner-state:
  adjust warmth/brevity/pacing only; never moralize, diagnose, or persist a
  mood label into long-lived memory. `UserStateOut` gains no affect field
  unless a consented, labeled design lands with it.

Multi-device sync via home station:

- Home station stays the Postgres authority; devices fetch via
  `/v1/everywhere/bootstrap` + `/v1/everywhere/changes` (v2 cursor,
  epoch mismatch → fresh bootstrap). Owner-model rows sync only under
  VISIBLE prefixes and the owner-trusted sensitive gate; model tokens/PCM
  never cross. Offline captures keep the 201/409/422/queue contract.

## 6. Phased build

### Phase 0 — Eval skeleton + compat proof (no behavior change)

- Seam files: `backend/app/scripts/eval_gates.py` (append-only gate fns),
  `backend/tests/test_memory_foundation.py` (read-only contract).
- New tables/endpoints/settings: none. New gate names only:
  `owner_fact_recall`, `owner_provenance_answer`, `owner_state_no_moralize`,
  each SKIP-first (missing artifact → SKIP with reason).
- Tests+gates: new gate unit tests mirroring `test_eval_gates.py:213-252`
  (pass-on-fixture, skip-without-artifact, skip-when-degraded);
  `api_contract`, `retrieval`, `continuity` must stay green.
- Rollback: delete the three gate functions; report shape unchanged.
- Compat proof: no manifest delta (`make update-contract` produces no diff);
  `make eval` exit code unchanged from the RED baseline (latency +
  regression FAIL pre-existing, owned elsewhere).

### Phase 1 — Owner model store (Agent 9 seam; Agent 16 DEPENDENCY NOTE for state)

- Seam files: `backend/app/models.py` (append tables), `backend/app/schemas.py`
  (append Owner*Out), `backend/app/ev/memory_ops.py` (extend, do not rewrite).
- New tables (append-only migration): `owner_traits`, `owner_values`,
  `owner_thinking_style` (all: id, version_group, version, supersedes_id,
  is_current, text, confidence, source_type, privacy_level, valid_from/until,
  fingerprint) + `owner_model_events` provenance join + `owner_state_snapshots`
  (TTL'd, no version chain). Single-owner: `owner_id` nullable, reserved for
  a future second identity (per §16.4 additive rule).
- New endpoints (additive): `GET /v1/owner/model`, `POST /v1/owner/correction`,
  `POST /v1/owner/forget`, `POST /v1/owner/restore` (all require owner trust;
  forget/restore mirror `memory_ops` hide/reverse semantics).
- New settings: `EV_OWNER_MODEL_ENABLED` (default false),
  `EV_OWNER_STATE_TTL_S` (default 3600). Append in agent block; document in
  `docs/ENVIRONMENT.md` agent block.
- Tests+gates: writer tests (dedupe-no-new-row, supersede chain, provenance
  row, correction v+1 @1.0, forget-hide/restore-reverse, rebuild replay);
  `roadmap` + `api_contract` (with Agent 1 regen in same commit) green.
- Rollback: flag off → endpoints 404 via router guard; tables stay empty;
  rebuild ignores them when flag off.
- Compat proof: contract delta is purely additive (new paths only);
  `OwnerIdentity`/`AssistantProfile`/`personality.py` untouched (read seams).

### Phase 2 — Capture → distill owner facts (Agent 9 OWNS)

- Seam files: `backend/app/memory/extraction.py` (new owner-fact patterns),
  `backend/app/contracts.py` (`MemoryCandidate` gains `owner_kind`
  nullable — additive field), `backend/app/services/processor.py`
  (route owner candidates to owner writer).
- New tables/endpoints/settings: none beyond Phase 1. Setting:
  `EV_OWNER_DISTILL_MODE` off|shadow|on (default shadow: ledger only).
- Tests+gates: extraction tests for trait/value/thinking-style phrasing;
  shadow-mode test asserts no rows written; ON-mode test asserts versioned
  rows + provenance; `test_memory_foundation` F0/F1 contract green.
- Rollback: mode → shadow/off; no writer path runs.
- Compat proof: `MemoryCandidate` change is optional-field-only; existing
  extractor tests unmodified and green; retrieval untouched (Agent 8 seam
  not crossed).

### Phase 3 — Retrieve → apply with owner relevance (Agent 8 seam; DEPENDENCY NOTE)

- Seam files: `backend/app/memory/retrieval.py` (multiplier hook only),
  `backend/app/training/personalization.py` (owner-type calibrations),
  `backend/app/filter/input_filter.py` (`compile_context_block` owner block).
- New tables/endpoints/settings: none. Settings:
  `EV_OWNER_RETRIEVAL_BOOST_MAX` (default 1.2, clamped ≤1.2),
  `EV_OWNER_CONTEXT_TOKENS` (default 800, inside `EV_CONTEXT_BUDGET_TOKENS`).
- Tests+gates: multiplier-{}-without-consent test; capped-boost test;
  SCORE_WEIGHTS-sum test still green; context-budget test (owner block never
  exceeds its token cap); `retrieval`, `retrieval_quality` (SKIP offline),
  `regression` (no >1 rank drift) green.
- Rollback: boost max → 1.0 and context tokens → 0 via settings; ranking
  returns to locked formula bit-for-bit.
- Compat proof: formula constants untouched; weight overrides remain
  renormalized multipliers; no new /v1 route, no manifest delta.

### Phase 4 — Provenance in answers + correction UX (Agent 9 + Agent 16 seams)

- Seam files: `backend/app/filter/output_filter.py` (chip stage),
  `backend/app/api/ev.py` or `tools.py` (append-only chip surface),
  `backend/app/ev/memory_ops.py` (owner correction path).
- New endpoints (additive): `GET /v1/owner/model/{row_id}/audit` (version
  chain + source events). No new tables.
- Tests+gates: chip-presence test (every memory-backed claim cites
  memory+event ids); unsupported-claim-removed test; correction e2e
  (POST correction → new current version → old answer's chip resolves to
  superseded row); `filter`, `continuity`, `grounding` green.
- Rollback: chip rendering behind `EV_OWNER_CHIPS_ENABLED` (default true
  after bake; false restores prior output shape).
- Compat proof: output-filter stage order unchanged (chips stay in the
  existing chip stage); endpoint additive with Agent 1 regen.

### Phase 5 — State-aware guidance + home-station sync (Agent 16 + sync owner seams)

- Seam files: `backend/app/ev/user_state.py` (state snapshot builder),
  `backend/app/ev/interaction.py` (policy table extension),
  `backend/app/everywhere/sync.py` (VISIBLE prefix entry for owner rows).
- New tables/endpoints/settings: none beyond Phase 1. Settings:
  `EV_OWNER_STATE_ENABLED` (default false),
  `EV_OWNER_STATE_CONSENT_TRACK` (default `life_data_personalization`).
  Sync: `GET /v1/everywhere/owner/changes` (additive, cursor-paged like
  `changes`); state snapshots NEVER sync (device-local, TTL'd).
- Tests+gates: no-moralize test (banned phrase list + diagnosis-pattern
  scan on guided outputs); state-TTL test (expired snapshot excluded);
  sync tests (bootstrap includes owner rows iff owner-trusted; epoch
  mismatch → STATE_EPOCH_MISMATCH); `continuity`, `api_contract` green.
- Rollback: state disabled → policy table falls back to existing emotions;
  owner sync prefix removed → devices keep last bootstrap, no deletes pushed.
- Compat proof: `UserStateOut` gains no field in v1 (snapshot is internal);
  sync delta additive; sandbox devices still isolated per `owner.py:36`.

## 7. Data model + API deltas (additive-only)

Tables (all new; no column changes): `owner_traits`, `owner_values`,
`owner_thinking_style`, `owner_model_events`, `owner_state_snapshots` (§6.1).
Indexes mirror `memories`: `(version_group, version)`, `is_current`,
`privacy_level`. Migration sets `down_revision` to the Alembic head at start;
SQLite upgrade clean (no `CREATE EXTENSION`).

Endpoints (all new; no signature changes):

- `GET /v1/owner/model` — current owner rows + state snapshot (owner trust).
- `POST /v1/owner/correction|forget|restore` — lifecycle mutations (owner
  trust; forget/restore reversible; raw events preserved).
- `GET /v1/owner/model/{row_id}/audit` — version chain + source events.
- `GET /v1/everywhere/owner/changes` — cursor-paged owner sync delta.

Contract: all deltas land with `make update-contract` regen by Agent 1 in the
same commit; `no_unlocked_v1_routes` FAILS otherwise. `docs/API.md` additive
rule holds (breaking → new major + capability negotiation).

## 8. Eval plan

Keep green (must-stay-green set): `api_contract`, `retrieval`,
`retrieval_quality`, `regression`, `continuity`, `latency`, `roadmap`,
`grounding`, `filter`, `camera_memory` + the 30+ memory/fetching test files
(`test_memory_foundation`, `test_continuity`, `test_retrieval_quality`, …).

Continuity (existing, do not weaken): kernel sends `[system,user]` with no
history; bare "yes" binds only via armed offer + turn ledger (9 checks,
`eval_gates.py:1708-1816`). Owner work must not displace the offer or ledger.

New owner-recall gates (SKIP-first, degraded-SKIP):

- `owner_fact_recall`: seeded owner facts (trait/value/decision) recalled
  top-5 ≥ 90% + nDCG@10 ≥ 0.80 on a checked-in fixture; missing artifact or
  `degraded=true` (hash/echo/mock providers) → SKIP with reason.
- `owner_provenance_answer`: every memory-backed claim in fixture answers
  carries a resolvable memory+event chip; unresolvable → FAIL; degraded →
  SKIP.
- `owner_state_no_moralize`: guided outputs on stressed/tired fixtures
  contain zero banned moralizing/diagnosing patterns; classifier-absent →
  SKIP (regex floor still tested in-process).

Harness stays hermetic: temp SQLite, sync mode, hash/echo doubles,
`EV_SEARCH_PROVIDER=none` (`eval_gates.py:2650-2684`).

## 9. Risks + WHAT IS STILL NOT REAL

Risks:

- Tree is RED at baseline (`last-run.json`: latency + regression FAIL) —
  owner work must compare per-gate against a HEAD worktree, not against green.
- Unknown embedding provider silently falls back to hash
  (`embeddings.py:602-603`); an owner-relevance threshold measured on hash is
  not a quality number — gates must SKIP, and the fallback deserves a
  DEPENDENCY NOTE to Agent 8 rather than a local fix.
- `HTTPEmbeddingProvider` has no degraded path; only the Retriever
  try/except (`retrieval.py:256-260`) saves it (semantic → 0.0). Owner boost
  must be defined to survive `query_emb=None`.
- Retrieval-adjacent layers (`memory/recall.py`, `router.py`, `intent.py`)
  are uninspected — Phase 3 must read them before touching the seam.
- macOS menu-bar ingest entry point and iOS baseURL provisioning are
  unlocated — Phase 5 sync work must locate both or name the gap.
- `test_oauth_calendar` flaky; `test_digital_operations` order-sensitive —
  run owner tests isolated AND in-suite before claiming green.

WHAT IS STILL NOT REAL:

- Owner traits/values/thinking-style tables, writer, and audit surface do
  not exist (`OwnerIdentity` holds only `display_name`).
- Owner-fact distillation patterns and the `owner_kind` candidate field do
  not exist; shadow-mode ledgering is not built.
- Consent-gated owner retrieval multipliers and the bounded owner context
  block do not exist; ranking today knows no owner.
- Provenance chips citing owner rows, and owner correction/forget/restore
  endpoints, do not exist.
- State-aware guidance beyond keyword activity + regex emotion does not
  exist; no classifier, no policy table for owner-state, no TTL snapshots.
- Owner home-station sync (prefix entry + delta endpoint) does not exist;
  state snapshots are specified as never-syncing but no code enforces it.
- The three new eval gates and their fixtures do not exist (Phase 0
  specifies them SKIP-first).
