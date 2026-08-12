# Memory Engine — Agent 9 (MNEMO)

This document covers the memory extraction, entity resolution, conflict,
rollup, and context-budget work owned by Agent 9. The invariants below are the
product and are enforced by tests, not by intent.

## Invariants (do not break)

- Raw events are append-only; deletion is tombstone-only.
- Every memory traces to ≥ 1 raw event via `memory_events`.
- Version chains preserve v1 with `valid_from` / `valid_until` / `supersedes`.
- Dropping all derived data and regenerating from events yields an equivalent
  state (`POST /v1/memory/rebuild`, tested by `test_memory_rebuild.py` and the
  import round-trip).
- `never_send_to_model` is excluded at the retrieval boundary and is also
  excluded from the optional LLM extraction pass.
- `source_type ∈ {explicit, inferred, derived}`; inferred claims are phrased
  as observations, never as facts.

## Extraction

`backend/app/memory/extraction.py` is the deterministic rule-based default and
the ingestion hot path. It emits `episodic` plus typed candidates
(decision/preference/goal/fact/observation), attaches entity refs, and now
attaches resolved temporal expressions to candidate payloads. Typed extraction
runs per sentence/clause (splitting on sentence boundaries and on
`and/but/so/then` before a subject pronoun), so multi-statement captures like
"I decided to go with Postgres. Also, I prefer morning runs." yield one memory
per statement. Contractions and casual phrasings are covered: "I've decided
to…", "I'm planning to…", "I'd rather X than Y", pronoun-less "decided to…" /
"prefer X", "goal: …", "I'm 29 years old", "my favorite city is …".

### LLM-assisted extraction (optional, fail-closed)

`backend/app/memory/llm_extractor.py` implements the LLM extractor behind the
existing extraction seam (Follow-up Order 6 topology). The local brain is no
longer available on the owner's M2/8 GB, so enrichment routes through the
configured chat provider — the DeepSeek API when `EV_CHAT_PROVIDER=deepseek` —
and is enabled only with `EV_LLM_EXTRACTION_ENABLED=true`. Offline CI never
needs a model or network: without the env flag (or with an unsupported
provider), everything returns `None`/`[]`.

Ingestion never blocks on or fails from a network call: rule-based memories
are written synchronously and are the floor. Enrichment is an asynchronous
pass (`backend/app/services/llm_extraction.py`) that runs as a separate
job/backfill with four deliberate cost controls:

1. **Batching** — many captures per API call (`EV_LLM_EXTRACTION_BATCH_SIZE`,
   default 8).
2. **Deduplication** — a content-hash cache (`source_text_fingerprint` in the
   `extraction.llm` event metadata) ensures identical/near-identical text is
   never re-extracted.
3. **Triage** — only captures the rules did not clear confidently, or
   long/complex multi-sentence captures, are sent (`should_enrich`). Short,
   clear captures never touch a paid API.
4. **Budget cap** — hard daily/monthly call and token caps
   (`EV_LLM_EXTRACTION_DAILY_CALL_CAP` / `_TOKEN_CAP` and monthly variants);
   when exhausted, enrichment pauses and the rule path continues silently.
   Every enrichment call is audited through `model_calls` (actor `memory`), so
   Agent 10's cost meter can read the same ledger. `GET /v1/enrichment/usage`
   exposes current usage, caps, and paused state; the batch backfill also has a
   scheduler-ready entrypoint (`run_llm_extraction_batch_job`).

Structured output is stored as an immutable `extraction.llm` raw event; rebuild
replays that event deterministically instead of re-invoking the model,
preserving the rebuild invariant. Output includes memory type, text, entities,
importance, confidence, and `source_type`; invalid or fabricated fields are
rejected. The `extraction.llm` event is timestamped after its source event so
rebuild replay order always matches live ingestion order.

**Measured economics (synthetic seed):** triage sends ~10 of 49 captures;
with batch size 8 that is **4.08 API calls per 100 captures**, and at the
default assumptions (3,000 captures/month, 1,200 tokens/call, $0.50/M blended
tokens) the estimate is **~$0.07/month**. Numbers are configurable via
`EV_LLM_EXTRACTION_*` env vars and reported by
`measure_enrichment_economics`.

### Temporal expressions

`backend/app/memory/temporal.py` resolves relative expressions ("last Tuesday",
"in March", "two years running", "since 2023", "in the last three months") to
absolute UTC instants anchored at the event's own timestamp, so resolution is
deterministic. Results are stored in candidate payloads under `temporal`
(`start`, `end`, `kind`) and survive rebuild unchanged.

`GET /v1/temporal/memories?period_start=…&period_end=…` is the as-of query
surface: it returns current memories whose resolved temporal range (or event
time) overlaps the requested period, so "what was I thinking in March?" is
answered from stored data.

## Entity resolution

`backend/app/memory/entities.py` provides:

- Accent/case/punctuation normalization for canonical keys.
- Alias tables on `Entity` plus generic nickname families as *candidate hints*
  only — nothing merges silently.
- `find_entity_candidates` with exact/alias/nickname/token/embedding-similarity
  ranking (embedding candidates use Agent 8's embedder when provided).
- `resolve_entity_ref` maps incoming refs to existing aliases deterministically.
- Human-confirmed merge via `POST /v1/entities/merge`. The merge is recorded as
  an `entity.merge` raw event (target/absorbed canonical keys), so rebuild
  replays it into an equivalent state; memory links, relationships, and
  recognition logs are re-pointed and the absorbed identity becomes an alias.
  Merge summaries store a canonical UTC-naive timestamp, so the absorbed
  entity's marker is identical before and after a rebuild.
- `GET /v1/entities/duplicates` reports the duplicate-entity rate over linked
  entities (exact name, nickname family, or token-overlap ≥ 0.8 components)
  before and after merges.

## Conflicts

Open conflicts are created (never silently arbitrated) for contradictory
observations, facts (same subject+property, different value), preferences
(same subject with opposite polarity, or a reversed pair), and decisions
(overlapping topic, different decision). `GET /v1/conflicts` lists them, and
`app/services/conflicts.py` surfaces open conflicts into the context window so
the model asks which version is current instead of picking one.

## Long-horizon rollups

`POST /v1/state-of-me` derives a "state of me" summary for a period from
versioned decision/goal/fact/preference memories only — counts, version-chain
changes, latest text per thread, and provenance to the source events. Each run
is recorded as a `rollup.run` event and rebuilt deterministically: thread rows
are ordered by content (type, first valid-from, latest text), not by
regenerated UUIDs, so the summary payload is byte-identical after a rebuild.

## Context compiler

`ContextCompiler.compile_progressive` starts with a shallow memory slice and
widens only when the question signals a deep dive and the budget has headroom.
`budget_adherence_report` measures per-question utilization; the 50-question
harness in `test_context_compiler.py` asserts zero over-budget plans and
reports p95 utilization.

## Evaluation

`backend/eval/extraction/seed_captures.json` is a CI-safe synthetic labeled
corpus (49 captures / 50 labels, including messy multi-sentence, contraction,
casual-phrasing, and deliberately hard cases). `tests/test_extraction_quality.py`
scores every `*.json` corpus in that directory and reports **three rows**:
rule-only, rule+LLM enrichment (perfect, triaged oracle), and the delta — so
the owner can see exactly what API spend buys. The product acceptance numbers
require the human-owned 100-capture hand-labeled set: drop a
`real_captures.json` into the same directory (schema in
`backend/eval/extraction/README.md`) and the harness reports it automatically.
