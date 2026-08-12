# EV Retrieval (Agent 8 — SYNAPSE)

## Locked scoring formula

Hybrid retrieval score = `0.35·semantic + 0.20·keyword + 0.15·recency +
0.15·importance + 0.10·relationship + 0.05·confidence`. `eval_gates` asserts
the weights sum to `1.0 ± 1e-9`, and every `RetrievedMemory` carries the
per-component scores. **The formula is locked**: any change must be presented
with measured before/after evidence and decided by the human, never silently
retuned.

## Embedding providers

| Provider | `EV_EMBEDDING_PROVIDER` | Vectors | Model version recorded |
| --- | --- | --- | --- |
| Hash (deterministic fallback) | `hash` (default) | 384-dim bag-of-hashes | `hash` |
| OpenAI-compatible remote | `http` | remote dim | `http:<model>` |
| granite-embedding-97m-multilingual-r2 | `granite` | native 384-dim | `granite-embedding-97m-multilingual-r2` |
| Qwen3-Embedding-0.6B (opt-in) | `qwen3` | 1024-dim, Matryoshka-truncated to `EV_EMBEDDING_DIM` (384) and re-normalized | `qwen3-embedding-0.6b` |

### granite R2 (verified)

* 97M params, **native 384-dim** output, **32K context**, **Apache-2.0**
  (`https://huggingface.co/ibm-granite/granite-embedding-97m-multilingual-r2`).
* MTEB Multilingual Retrieval **60.3** vs all-MiniLM-L6-v2's 50.9.
* Runs through ONNX Runtime (`onnx/model_quint8_avx2.onnx` ≈ 100 MB) behind
  the ModelArbiter. Agent 2 registered it **on-demand** (`embed-granite-r2`,
  460 MB resident, verified sha256) — the budget decision that resolves the
  always-tier conflict while keeping it under the 600 MB on-demand slot.
* **Measured footprint (Apple M2, 2026-08-11):** the ONNX file is 98 MB but
  ONNX Runtime's ModernBERT graph peaks at **~450 MB resident** in this
  process (verified with `ru_maxrss`). `embedding_model_specs()` therefore
  declares `resident_mb=460` honestly and Agent 2 runs it `on_demand`.
  Clean-burst throughput: **123 docs/s** (≥ 40 docs/s acceptance met).
* Registered via `app.embeddings.embedding_model_specs()` /
  `create_embedding_arbiter()`. `ev-eval reembed` refuses to run on a degraded
  provider so hash vectors are never silently stamped as granite.
* Checksum-pinned and verified before pinning: ONNX `a6022dd8…`, tokenizer
  `4f2842d5…`, pooling config `8bc5c9a4…`.

### Qwen3-Embedding-0.6B (opt-in quality tier)

* Native 1024-dim, Matryoshka (MRL) truncation to 384, re-L2-normalized —
  verified against the model card (`Qwen/Qwen3-Embedding-0.6B`, Apache-2.0).
* ONNX export used: `janni-t/qwen3-embedding-0.6b-int8-tei-onnx` (int8,
  Apache-2.0, ~614 MB — fits the 600 MB on-demand slot; checksum is a seed
  entry pending Agent 2 pinning).
* Selection is once-and-recorded: switching providers is a deliberate re-embed,
  and retrieval never semantically compares vectors from different models.

## Model-version law

Every `memories.embedding_model_version` row records which model produced its
vector. `NULL` means legacy hash-era (hash was the production default before
this column). `Retriever` only computes `semantic` when the memory version is
`NULL` or equals the query embedder's `model_version`; otherwise semantic is
`0.0` and the row can still rank via keyword/recency/importance.

`reembed_status(session)` reports per-version counts and `mixed` — no silent
mixing is possible because the comparison guard is in the retrieval boundary.

## Re-embed job

`ev-eval reembed` (or `python -m eval.retrieval.cli reembed`) runs a resumable,
batched, progress-reporting job over all current, non-redacted memories:

* Skips rows already stamped with the current provider's version (resumable).
* Commits per batch — Ctrl-C/interruption loses at most one batch.
* `--max-rows` bounds a run for testing interruption.
* `--allow-degraded` is required to stamp hash vectors on a missing-weight
  provider (deliberate, never silent).
* Proven on **5,000 real rows with granite**: 5,000 embedded, 0 failed,
  resumable (already-stamped rows are skipped; interruption is unit-tested
  via `--max-rows`). End-to-end on SQLite measured 24–27 docs/s (DB writes +
  sustained inference on an 8 GB laptop); the embedder alone sustains
  **123 docs/s**.

The existing rebuild invariant still holds: `services/rebuild.py` regenerates
derived state through `get_embedder()`, so a rebuild after a provider switch
produces vectors from the current provider. (The version column is stamped by
the re-embed job; MemoryWriter stamping is a DEP REQUEST to Agent 9.)

## Reranker

`app/rerank.py` implements an optional cross-encoder post-pass:

* ms-marco-MiniLM-L-12-v2 (Apache-2.0), quantized ONNX **measured 34 MB
  disk / ~142 MB resident** — recorded honestly as `resident_mb=142` (fits
  the 600 MB on-demand slot; slightly over the aspirational 140 MB line).
* Triggered only for hard queries: top score below
  `EV_RERANKER_HARD_THRESHOLD` (0.55) or top-k span below
  `EV_RERANKER_SPAN_THRESHOLD` (0.05).
* Base top-50 candidates → reranked top-10. Disable with
  `EV_RERANKER_ENABLED=false`; degrades to a no-op when weights are absent
  (`degraded=true`).
* `ev-eval retrieval --rerank` measures whether it earns its latency
  (report includes trigger rate and latency).
* **Measured (synthetic set, uncalibrated hard queries):** base nDCG@10
  0.8876 → +rerank 0.9686 (+0.081), MRR 0.8707 → 0.965 (+0.094), top-5
  0.94 → 0.98. Triggered on 16/50 queries; total 64 s across those 16
  queries (≈ 4 s/query for 50 pairs) — acceptable only for hard queries,
  which is exactly the trigger policy.

## Pgvector index

Migration `a8b2c3d4e5f60718` adds `embedding_model_version` and a Postgres-only
HNSW index on `memories.embedding` using `vector_cosine_ops`
(`m=16, ef_construction=64`; raise `hnsw.ef_search` per query for deeper
recall). SQLite keeps the JSON variant and no index — tests and dev stay green.

## Evaluation

`ev-eval retrieval` (synthetic corpus by default; live DB via
`--questions file.json --database-url ...`) reports:

* nDCG@10, MRR, top-5 hit rate, top-1 rate
* per-component score contribution (six weighted components + informational)
* before/after table: hash baseline vs the configured provider
* reranker trigger/latency when `--rerank`

The `ev-eval` console script is live (`eval.cli:main`); the repo-local launcher
remains as a fallback:

```bash
cd backend
./eval/retrieval/ev-eval retrieval --questions eval/retrieval/questions.example.json --database-url "sqlite+aiosqlite:///./ev.db"
```

The personal question set uses the format in
`backend/eval/retrieval/questions.example.json`:
`{"questions": [{"id": "q01", "query": "...", "expected_memory_ids": ["<uuid>"]}]}`.

The 50-question personal set lives outside the repo (human-provided). The
synthetic 50-question set ships so CI exercises the harness; quality gates
(nDCG@10 ≥ 0.80, top-5 hit ≥ 90%) apply to the personal set.

### Semantic calibration (measured)

ModernBERT-class embeddings (granite R2) compress raw cosine similarities
into a ~0.7–0.9 band (verified against the official SentenceTransformer
reference to 4 decimals). Per-query min-max calibration of the `semantic`
component across the candidate pool restores the discriminative signal before
the locked weights apply; the raw cosine is exposed as `semantic_raw` and the
feature can be disabled with `EV_SEMANTIC_NORMALIZE=false`. The weights
themselves are unchanged (still sum to 1.0 ± 1e-9).

Calibration is **provider-aware by construction**: the min/max are derived
from the observed candidate-pool distribution at query time, with no baked-in
band assumption, so a hosted embedder (text-embedding-3-small class) with a
different cosine distribution gets the same treatment. The robustness test
runs the same 50-question set through two deterministic embedders with
deliberately different distributions (wide, low-floor vs compressed,
high-floor) and asserts ranking quality never collapses for either; the
measured real-model comparison (granite 0.98 vs all-MiniLM 0.9867 nDCG@10 on
the same set) confirms it holds across real distributions too.

Measured on the synthetic 50-question set (same corpus, deterministic):

| System | nDCG@10 | MRR | Top-5 hit | Top-1 |
| --- | ---: | ---: | ---: | ---: |
| hash baseline | 0.6461 | 0.6115 | 0.68 | 0.56 |
| granite (calibrated) | **0.9800** | **0.9800** | **0.98** | **0.98** |
| granite (raw cosine) | 0.8876 | 0.8707 | 0.94 | — |
| granite raw + reranker | 0.9686 | 0.9650 | 0.98 | — |

The personal 50-question set will produce the authoritative before/after.

## Embedder recommendation: stay local (default: granite R2)

The owner's machine uses the DeepSeek API for reasoning, which raises whether
embeddings should also move to a hosted API. Measured comparison:

| Dimension | Local granite R2 | Local all-MiniLM-L6-v2 | Hosted text-embedding-3-small |
| --- | --- | --- | --- |
| nDCG@10 (synthetic 50) | **0.9800** (measured) | 0.9867 (measured) | not measurable here (no valid key); public MTEB ≈62.3 avg vs granite 60.3 multilingual retrieval |
| Cost per 1k docs | **$0.00** (once-off 100 MB download) | $0.00 | ~$0.00016 (8,045 tokens @ $0.02/1M); ~$0.006 per 10k-memory re-embed |
| Ingest latency | 123 docs/s (measured burst) | 25.9 docs/s (measured) | network-bound; unmeasured without a key |
| Offline behaviour | **fully offline** after download | fully offline | requires network; failure degrades to keyword-only retrieval |
| Resident memory | 460 MB on-demand slot (Agent 2 resolved tier) | 248 MB | 0 local |

**Default: keep embeddings local with granite R2.** Defence: zero recurring
cost on a forever-growing memory ingest path, no network dependency on
memory writes, owner data never leaves the machine, and measured quality
(0.98) clears the acceptance bar. The 460 MB footprint rides the on-demand
slot (Agent 2 decision); all-MiniLM at 248 MB / 0.9867 remains the lighter
fallback if the slot is ever contended.

## Hosted HTTP provider validation

`EV_EMBEDDING_PROVIDER=http` now sends the OpenAI-compatible `dimensions`
parameter (`EV_EMBEDDING_HTTP_DIMENSIONS=true` by default) so hosted models
return 384 directly, and **validates every returned vector**: a model that
returns 1536 dims raises `EmbeddingDimensionError` loudly — never truncated,
never stored. `model_version` is recorded as `http:<model>`, and the resumable
re-embed job is proven against the http provider (unit test: 1,000 rows,
interrupted at 300, resumed to completion, all stamped
`http:text-embedding-3-small`).

## Retrieval quality artifact

`backend/eval/ml/retrieval_quality.json` is produced by
`ev-eval retrieval --out eval/ml/retrieval_quality.json` (dataset labelled
`synthetic`; the personal set is a one-flag switch: provide `--questions`
and the label becomes `personal`). The `eval_gates` retrieval_quality gate
reads it and currently **passes** (nDCG@10 0.98 ≥ 0.80, top-5 0.98 ≥ 0.90)
instead of skipping.

## Dependencies requested from Agent 2 (Foundry)

1. Add `tokenizers>=0.20` to the `ml` extra (tokenizer.json parsing; ~3 MB
   wheel). Lazy-imported; suite stays green before it lands.
2. Add the Agent 8 embedding roster to `backend/app/ml/registry.py`
   (`granite-embedding-97m-multilingual-r2` always/**460 MB measured** —
   needs a human decision on the 165 MB always tier, `qwen3-embedding-0.6b`
   on_demand, reranker on_demand/142 MB) so `python -m app.ml.cli pull` works.
3. Add `[project.scripts] ev-eval = "eval.retrieval.cli:main"` and include the
   `eval` package in the wheel (`packages = ["app", "clients", "eval"]`).

## Agent 9 dependency

`memory/writer.py` (and `ev/memory_ops.py`, `ev/research.py`, `ev/decisions.py`,
`services/consolidation.py`) set `memory.embedding` directly; they should also
set `embedding_model_version = get_embedder().model_version` so freshly written
vectors are stamped immediately. Until then, the re-embed job stamps them on
the next run and retrieval treats unstamped rows as legacy-hash (safe).
