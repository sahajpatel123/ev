# EVIE Training Runbook (Domain 7) — FORGE

EVIE's training work is consent-gated, evidence-backed, versioned, reversible,
and split into **two honest categories**.

## 0. Trainable vs servable — read this first (30 seconds)

- **Not trainable:** DeepSeek V4 Flash. It is a hosted API model — no local
  weights, no adapter API. Anyone claiming to train EV on DeepSeek V4 Flash is
  talking about prompting, not training.
- **Trainable AND servable today (zero GPU):** prompt-level personalization —
  the style profile, importance calibration, and filter recalibration. They
  learn from the owner's real ratings and corrections, apply at the prompt and
  scoring layer on every request against the hosted model, and cost nothing to
  train. This is EV's **active** training story.
- **Trainable but staged:** LoRA/DPO weight training (mlx-tune). The machinery
  is built and proven on this Mac, but the M2/8 GB cannot host local LLM
  inference, so reasoning runs through the DeepSeek API — a locally trained
  adapter has **nowhere to load**. Weight training is therefore staged until a
  self-hosted inference target exists, and `run_training` refuses to train when
  none is configured rather than producing an artifact that cannot be served.

The two sets are different today. When a servable local model exists, the
weight-training pipeline (SFT + DPO, eval, rollback, privacy) is ready to use
with owner data.

## 0.1 HUD surface learning (added)

The window UI is **not a frozen skin**. `eval/hud/surface_corpus.json` is a
one-time public harvest (cited URLs + licenses in
`eval/hud/public_sources.json`; multiple paraphrases per mechanic plus a
held-out slice) and `eval/hud/surface_calibration.json` is the
repo-shipped fit (JARVIS sizes, Karen time-types, E.V.I.E. less-intrusive rule).
This does **not** weight-train DeepSeek. It is a deeper policy fit, scored on
phrases the fit step did not consume.
`app/training/surface.py` scores the live planner against that gold set
(`GET /v1/training/surfaces/smoke`) and folds owner ratings
(`POST /v1/training/surfaces/rate` → `POST /v1/training/surfaces/calibrate`)
into urgency, suppress/boost kinds, and size preference. That calibration is
what `plan_surfaces` reads on every turn.

This is the same class of training as style/importance/filter: evidence in,
policy out, reversible by writing a new version. It is **not** DeepSeek
weight training. The corpus also exports as SFT/tool records
(`surface.sft_records`) for a future local adapter.

Review the current renderer at `/app/gallery`. Rate a window that felt wrong
and calibrate — the next HUD decision learns.

## 1. The active training story — prompt-level personalization

Three deterministic learners turn the owner's real ratings and corrections
into behavior **at the prompt/scoring layer**, on every request, with no GPU
and no training run:

| Learner | Learns from | Applies where | Consent | Reversible |
| --- | --- | --- | --- | --- |
| Style profile (`style_adapter.py`) | rated response logs + corrections | output-filter persona (`enforce_persona`) | `adapter_fine_tuning` | adapter rollback/delete |
| Importance calibration (`personalization.py`) | per-domain corrections/usefulness/follow-ignore | retrieval scoring (`Retriever.search` → `components["personalization"]`) | `life_data_personalization` | calibration rollback/revoke |
| Filter recalibration (`filter_improvement.py`) | filter/decision ledger | live filter policy (`active_policy`) + EV Sense | `filter_self_improvement` | recalibration rollback/delete |

### Traced end to end (request path)

1. **Style profile:** `POST /v1/training/corpus/build` → rated entries →
   `adapter.register` (eval gates) → `adapter.activate` (explicit) →
   `active_style_profile()` → `filter/pipeline.run_full_filter_pipeline` →
   `filter/output_filter.enforce_persona` → styled `final_text` on
   `POST /v1/filter/evaluate` and chat filter passes. Proven by
   `test_pipeline_pre_post_adapter_responses` and
   `test_filter_evaluate_draft_replay_applies_style_profile`.
2. **Importance calibration:** `POST /v1/training/personalization/calibrate` →
   evidence-gated `PersonalizationCalibration` →
   `memory/retrieval.py:calibration_multipliers` → `Retriever.search` applies
   `components["personalization"]` to every recall used by chat/tools. Proven by
   `test_personalization_derives_and_applies_importance_learning`.
3. **Filter recalibration:** `POST /v1/training/filter/self-improve` →
   ledger-derived proposals → `POST /v1/training/filter/recalibration/apply` →
   `filter/policy.active_policy` → consumed by `filter/pipeline` and EV Sense.
   Proven by `test_filter_recalibration_apply_stores_runtime_policy` and the
   rollback test.

Each is consent-gated (revoking consent stops application immediately),
evidence-gated (never invented from an empty ledger), and reversible (rollback
restores the prior state or neutral defaults).

## 2. What exists in this repository

- **Consent-gated corpus harvester** (`app/training/corpus.py`): versioned,
  deterministic, erasable snapshots. The double privacy filter excludes
  `never_send_to_model` and `sensitive` rows at harvest **and** re-checks at
  export; credentials are redacted in both layers.
- **JSONL exporter** (`corpus.py:dataset_records` / `format_records`): canonical
  input/output/signals plus three provider formats — `sft` (instruction/
  response), `preference` (chosen/rejected), and `tool` (tool-call teaching).
- **Three deterministic statistical learners** at the prompt/scoring layer:
  `style_adapter.py`, `personalization.py`, `filter_improvement.py`.
- **Versioned adapter registry** (`app/training/adapter.py`): registration,
  eval gates, activation, rollback, erasure. Actual weight training is delegated
  to a provider.
- **Real local weight trainer** (new, FORGE, **staged**): `MLXLoRAProvider` in
  `app/training/lora.py` runs mlx-tune (MLX-native, Unsloth-compatible API)
  against Qwen3-0.6B/1.7B with 4-bit QLoRA, short sequences, gradient
  checkpointing, progress streaming, interruption safety, and the
  `trainer-mlx-lora` exclusive arbiter tier. It is marked `staged=true` and
  `run_training` refuses to train until a self-hosted inference target
  (`EV_CHAT_PROVIDER=local` + `EV_LOCAL_MODEL_BASE_URL`) is configured.
- **Eval harness** (`app/training/eval.py`): held-out split, win-rate vs base,
  tool-call validity, HUD schema conformance, overfitting detection.
- **Operator CLI** (`python -m app.training.cli`): dry-run, train, evaluate,
  status, losses, rollback.

**What is not here:** torch/transformers/peft/accelerate/datasets/trl are not
runtime dependencies. mlx/mlx-lm/mlx-tune are declared as the optional `mlx`
extra (Agent 2's pyproject) and imported lazily. `LocalLoRAProvider` remains the
generic configured-command provider; `MLXLoRAProvider` is the real trainer.

## 3. Training tracks

| Track | What it learns | Consent track |
| --- | --- | --- |
| Voice enrollment | Speaker voiceprint (enrollment, not weights) | `voice_enrollment` |
| Life-data personalization | Per-domain importance from corrections/usefulness/follow-ignore | `life_data_personalization` |
| Adapter fine-tuning | EV's voice/style/working style from the conversation corpus | `adapter_fine_tuning` |
| Filter self-improvement | Monthly ledger-driven threshold recalibration | `filter_self_improvement` |
| Corpus harvesting | Versioned snapshots feeding the adapter | `training_corpus` |
| **Local LoRA (MLX)** | **Real weights via mlx-tune on Qwen3-0.6B** | `adapter_fine_tuning` |

Perception (speaker, face, ASR, TTS) is out of scope: those models are
pretrained and owned by Agents 3–7. FORGE never trains perception from scratch.

## 4. Dataset formats

`GET /v1/training/corpus/{version}/dataset?format=...` returns NDJSON
(`application/x-ndjson`). `format` is one of:

| Format | Shape | Use |
| --- | --- | --- |
| `canonical` (default) | `input` / `output` / `signals` / `source` / `hash` | generic, existing consumers |
| `sft` | `instruction` / `output` / `signals` / `source` / `hash` | SFT instruction tuning |
| `preference` | `prompt` / `chosen` / `rejected` / `signals` / `source` / `hash` | DPO preference learning |
| `tool` | `instruction` / `tool_calls` / `output` / `signals` / `source` / `hash` | teaching EV's tool schemas |

Deterministic ordering, per-record content hashing, and the double privacy
filter are preserved in **every** format. A planted secret and a
`never_send_to_model` row never appear in any export — proven by
`tests/test_lora_trainer.py::test_corpus_export_formats_never_leak_secrets_or_excluded_rows`.
The `sft` format additionally emits tool-teaching rows (output = the recorded
JSON tool call, tagged `signals.tool_teaching: true`) so the local model learns
EV's tool schemas, not just prose style.

Preference pairs come from filter-ledger `draft → final_text` rows (chosen =
final, rejected = draft) and from response rows carrying a `corrected_text`
signal. `tool_calls` are harvested from `ResponseLog.strategy` and
`FilterLedger.detail` when present.

## 5. Provider contract

`WeightTrainingProvider` is the boundary in `app/training/adapter.py`:

- `estimate()` is local and deterministic (no external call).
- `train()` is the only method allowed to contact a provider.
- `dry-run` validates the dataset and eval gates and returns a plan with
  estimated cost and required approvals; it never calls a provider.
- `train` resolves the named provider, re-checks gates, refuses remote
  processing without `EV_ALLOW_REMOTE_TRAINING=true`, refuses non-zero cost
  without `cost_approved=true`, and **refuses to start while a voice session is
  active**.

Providers:

- `mlx-lora` — **the real local trainer** (new, **staged**). Exclusive arbiter
  tier `trainer-mlx-lora` (2000 MB resident, 3500 MB peak), evicts all on-demand
  models, takes the global lock, and refuses if another exclusive holder is
  active. `run_training` additionally refuses while no self-hosted inference
  target (`EV_CHAT_PROVIDER=local` + `EV_LOCAL_MODEL_BASE_URL`) is configured.
- `local-lora` — runs a configured command (`EV_TRAINING_LOCAL_CMD`).
- `openai-fine-tune` — hosted API path, remote gate + cost approval required.

### MLXLoRAProvider details

- **Staged**: marked `staged=true`; training is refused until a servable local
  inference target exists. The dry-run estimate reports `staged: true` and
  `servable: false/true`.
- Base model: `Qwen/Qwen3-0.6B` (or 1.7B) via `EV_TRAINING_LORA_BASE_MODEL`.
- 4-bit QLoRA (`load_in_4bit=True`), rank 16, alpha 32, dropout 0.05.
- `max_seq_length=512`, batch size 1, gradient checkpointing on
  (`grad_checkpoint=True`).
- Training mode: `sft` (default) or `dpo` (`EV_TRAINING_LORA_TRAIN_MODE`).
  SFT rows are converted to ChatML messages so the model learns through its
  real chat template. DPO uses the `preference` dataset (chosen/rejected from
  filter-ledger draft→final correction pairs) with mlx-tune's native
  `DPOTrainer`; it refuses when no preference pairs exist.
- Runs through `app.ml` ModelArbiter: `arbiter.acquire("trainer-mlx-lora",
  release_on_exit=True)`. No model is loaded outside the arbiter.
- Progress streams to `<run>/status.jsonl`; trainer stdout is tee'd there too.
- Interruption: a `CANCEL` file is honored between stages; a partial run is
  never finalized (no `COMPLETE` marker → `finalize_adapter` refuses).
- **Overfitting floor**: training refuses below `EV_TRAINING_LORA_MIN_OWNER_PAIRS`
  (default **200** owner pairs) with a clear message.
- When MLX/weights are absent, the provider degrades to a deterministic double
  marked `degraded=true`, `simulated=true`, `real_weights=false`. The double
  never fabricates a win-rate (`win_rate: null`, `measured: false`) and is
  refused at activation.

## 6. Eval harness

Every adapter version ships numbers from `app/training/eval.py`:

- **Held-out split**: deterministic (source+hash keyed, seeded), 20% eval by
  default → 40 held-out prompts at the 200-pair floor.
- **Win-rate vs base**: adapter and base generate the same held-out prompts;
  the judge is the disclosed deterministic function
  (`0.6 × reference-match + 0.4 × style-profile alignment`), not a hidden
  LLM-as-judge. Method is included in every result.
- **Tool-call validity**: every JSON tool call in generated output is checked
  for a string name and parseable JSON arguments. Each eval also runs five
  explicit tool/HUD probe prompts against the adapter so validity is measured
  even when held-out prompts do not elicit tool calls.
- **HUD schema conformance**: embedded `ev.hud.*` JSON is checked for required
  fields (`schema_version`, `generated_at`, `title`).
- **Overfitting detection**: `val/train loss ratio > 1.15` while train loss
  falls → flagged.

Numbers (loss curves, win-rate, tool/HUD checks, overfit) are written to
`<run>/eval.json` and `losses.jsonl` and stored in the adapter's
`eval_metrics.training_run` on the API.

## 7. Runbook

### 7.1 Consent

```sh
curl -X POST "$EV_BASE_URL/v1/training/consent" \
  -H "Authorization: Bearer $EV_MASTER_KEY" \
  -d '{"track": "training_corpus"}'
curl -X POST "$EV_BASE_URL/v1/training/consent" \
  -H "Authorization: Bearer $EV_MASTER_KEY" \
  -d '{"track": "adapter_fine_tuning"}'
```

### 7.2 Build and inspect the corpus

```sh
curl -X POST "$EV_BASE_URL/v1/training/corpus/build" \
  -H "Authorization: Bearer $EV_MASTER_KEY"
curl -o corpus-v1.jsonl "$EV_BASE_URL/v1/training/corpus/1/dataset" \
  -H "Authorization: Bearer $EV_MASTER_KEY"
curl -o corpus-v1-sft.jsonl "$EV_BASE_URL/v1/training/corpus/1/dataset?format=sft" \
  -H "Authorization: Bearer $EV_MASTER_KEY"
curl -o corpus-v1-pref.jsonl "$EV_BASE_URL/v1/training/corpus/1/dataset?format=preference" \
  -H "Authorization: Bearer $EV_MASTER_KEY"
```

### 7.3 Dry-run (no external call, no weights)

```sh
curl -X POST "$EV_BASE_URL/v1/training/adapter/dry-run" \
  -H "Authorization: Bearer $EV_MASTER_KEY" \
  -d '{"corpus_version": 1, "provider": "mlx-lora"}'
```

Below 200 owner pairs this refuses with the overfitting-floor message.

### 7.4 Register

```sh
curl -X POST "$EV_BASE_URL/v1/training/adapter/register" \
  -H "Authorization: Bearer $EV_MASTER_KEY" \
  -d '{"name": "evie-mlx-v1", "provider": "mlx-lora",
       "base_model": "Qwen/Qwen3-0.6B", "corpus_version": 1}'
```

Registration runs the deterministic eval gates; the adapter is `approved` only
if they pass.

### 7.5 Train (staged: real weights, arbiter exclusive lock)

**Staged gate:** with the current configuration (no `EV_CHAT_PROVIDER=local`
inference target), `POST /v1/training/adapter/train` with provider `mlx-lora`
refuses with a clear explanation instead of producing an artifact with nowhere
to load. Configure a servable local target first (see §13) and re-run the dry
run to confirm `servable: true`.

```sh
curl -X POST "$EV_BASE_URL/v1/training/adapter/train" \
  -H "Authorization: Bearer $EV_MASTER_KEY" \
  -d '{"corpus_version": 1, "provider": "mlx-lora",
       "adapter_id": "<adapter-id>", "cost_approved": true}'
```

Expected wall clock on an Apple M2 (8 GB) for 200 owner pairs, seq 512,
0.6B 4-bit QLoRA: roughly **6–12 minutes** for SFT (3 epochs × ~160 steps ≈
480 steps at ~1.2 s/step) plus ~1–2 minutes for the 40-prompt win-rate eval
(2 models). 1.7B roughly doubles it. Progress is visible in
`~/.ev/adapters/runs/<run>/status.jsonl` (or `python -m app.training.cli status`).

DPO (preference training on filter-ledger draft→final pairs):

```sh
uv run python -m app.training.cli train \
  --dataset corpus-v1-pref.jsonl --format preference --train-mode dpo \
  --adapter-ref evie-dpo-v1
```

DPO refuses when no preference pairs exist, uses mlx-tune's native DPO loss
against the frozen base reference, and ships the same eval numbers.

### 7.6 Activate (explicit, re-verified, logged — never automatic)

```sh
curl -X POST "$EV_BASE_URL/v1/training/adapter/activate" \
  -H "Authorization: Bearer $EV_MASTER_KEY" \
  -d '{"adapter_id": "<adapter-id>", "reason": "apply learned voice"}'
```

Activation re-runs the eval gates against the bound corpus snapshot, requires a
completed training run with `real_weights=true` (a degraded double is refused),
and writes an `adapter_activate_reverified` access-log entry. No path applies
weights without this explicit human action.

### 7.7 Roll back (byte-identical base)

```sh
curl -X POST "$EV_BASE_URL/v1/training/adapter/rollback" \
  -H "Authorization: Bearer $EV_MASTER_KEY" \
  -d '{"adapter_id": "<adapter-id>", "reason": "regression"}'
```

LoRA adapters are separate files; the base model directory is never modified.
Rollback removes the active adapter pointer and re-verifies
`base_sha256_before == base_sha256_after` (recorded in the run manifest), so
inference returns to the byte-identical base. Operator equivalent:

```sh
cd backend && uv run python -m app.training.cli rollback \
  --adapter-ref ~/.ev/adapters/adapters/evie-mlx-v1-...
```

### 7.8 Erase

```sh
curl -X POST "$EV_BASE_URL/v1/training/adapter/delete" \
  -H "Authorization: Bearer $EV_MASTER_KEY"
curl -X POST "$EV_BASE_URL/v1/training/corpus/delete" \
  -H "Authorization: Bearer $EV_MASTER_KEY"
```

Redacts all snapshots/adapters; exports of redacted snapshots are refused.

## 8. Environment

All `EV_TRAINING_LORA_*` settings live in `app/training/lora.py::LoRASettings`:

| Env var | Default | Meaning |
| --- | --- | --- |
| `EV_TRAINING_LORA_BASE_MODEL` | `Qwen/Qwen3-0.6B` | Base model (HF id or local dir) |
| `EV_TRAINING_LORA_OUTPUT_ROOT` | `~/.ev/adapters` | Run + adapter registry root |
| `EV_TRAINING_LORA_MIN_OWNER_PAIRS` | `200` | Overfitting floor |
| `EV_TRAINING_LORA_MAX_SEQ_LENGTH` | `512` | Sequence length (memory) |
| `EV_TRAINING_LORA_RANK` | `16` | LoRA rank |
| `EV_TRAINING_LORA_LORA_ALPHA` | `32` | LoRA alpha |
| `EV_TRAINING_LORA_LORA_DROPOUT` | `0.05` | LoRA dropout |
| `EV_TRAINING_LORA_LEARNING_RATE` | `1e-4` | LR |
| `EV_TRAINING_LORA_NUM_EPOCHS` | `3` | Epochs |
| `EV_TRAINING_LORA_TRAIN_MODE` | `sft` | `sft` or `dpo` |
| `EV_TRAINING_LORA_MAX_STEPS` | `-1` | Step cap (`-1` = epochs) |
| `EV_TRAINING_LORA_BATCH_SIZE` | `1` | Per-device batch |
| `EV_TRAINING_LORA_GRAD_ACCUM_STEPS` | `1` | Gradient accumulation |
| `EV_TRAINING_LORA_WARMUP_STEPS` | `10` | Warmup |
| `EV_TRAINING_LORA_GRAD_CHECKPOINT` | `true` | Gradient checkpointing |
| `EV_TRAINING_LORA_USE_4BIT` | `true` | 4-bit QLoRA |
| `EV_TRAINING_LORA_EVAL_FRACTION` | `0.2` | Held-out fraction |
| `EV_TRAINING_LORA_SEED` | `42` | Split/train seed |
| `EV_TRAINING_LORA_FORCE_DOUBLE` | `false` | Force deterministic double |
| `EV_TRAINING_LORA_ARBITER_NAME` | `trainer-mlx-lora` | Arbiter registry name |

## 9. Budget

`trainer-mlx-lora` is registered in the locked roster (`app/ml/registry.py`,
see `docs/MODEL_BUDGET.md`): exclusive tier, 2000 MB resident, 3500 MB peak,
under the 2400 MB ceiling when combined with the 165 MB always pin. Training
holds the arbiter's global lock and evicts all on-demand models; voice sessions
take priority and training refuses to start while one is live.

## 10. Verification

```sh
cd backend
uv run pytest tests/test_training_adapter.py tests/test_training_corpus.py \
  tests/test_training_personalization.py tests/test_training_style_profile.py \
  tests/test_training_filter_improvement.py tests/test_lora_trainer.py -q
uv run pytest -q
uv run ruff check app clients tests && uv run mypy app clients
```

## 11. REPORT FOOTER (mandatory for every FORGE report)

Include the latest run's loss curves and win-rate:

```text
Loss curves: train=[...] val=[...]
Win-rate vs base (N held-out prompts, deterministic judge): X.XX (N)
Tool-call validity: V/T (…)
HUD conformance: H/C (…)
Overfit: detected / not detected (val/train ratio …)
```

## 12. Proof runs (2026-08-11, Apple M2 8 GB)

Both runs used deterministic synthetic EV-style corpora because the repository
does not yet contain 200 real owner pairs; the same code paths run unchanged on
the owner corpus once collected. Real mlx-tune weights were produced in both.
These runs prove the **staged** machinery; they are not served anywhere until a
self-hosted inference target exists.

**SFT (ChatML rows, Qwen3-0.6B-4bit, 2 epochs, 229 rows):**

```text
Loss curves: train=[74 points, last=0.120] val=[3 points, last=0.137]
Win-rate vs base (46 held-out prompts, deterministic judge): 0.9783 (45W/1T/0L)
Tool-call validity: 1/1 = 1.0
HUD conformance: 13/13 = 1.0
Overfit: not detected (val/train ratio 1.1417)
Peak memory: 0.606 GB; base_sha256_before == base_sha256_after
```

**DPO (native mlx-tune DPOTrainer, 200 preference rows, 8 steps):**

```text
Loss curves: train=[0.6945]
Win-rate vs base (10 held-out preference prompts): 0.1 (1W/9T)
real_weights=true; base_sha256_before == base_sha256_after
```

## 13. Trigger condition for resuming weight training

Weight training becomes worthwhile the day the owner has a self-hosted
inference target that can actually load and serve a trained adapter: a machine
with at least 24–32 GB unified RAM (a Mac Studio/M4 Pro class box, or a small
NVIDIA/AMD server) running an OpenAI-compatible local server such as Ollama or
llama.cpp, with a Q4-quantized instruct model in the 8B–14B class (for example
Qwen3-8B/14B, which is strong enough to be a genuine local brain rather than a
demo), plus a corpus of at least 2,000–5,000 rated owner pairs and filter
corrections (the current 200-pair floor is the minimum for a first run; 2k+
produces stable win-rates and much lower overfit risk), at which point the
staged pipeline — consent gates, DPO-ready corpus, eval harness, explicit
activation, and byte-identical rollback — already ships unchanged and the only
remaining work is pointing `EV_CHAT_PROVIDER=local` and
`EV_LOCAL_MODEL_BASE_URL` at that server and running one training command.
