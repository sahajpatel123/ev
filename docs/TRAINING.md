# EVIE Training Runbook (Domain 7) — FORGE

EVIE's training work is consent-gated, evidence-backed, versioned, reversible,
and split into **two honest categories**.

## 0. Trainable vs servable — read this first (30 seconds)

- **Not trainable:** the hosted brain (Muse Spark via OpenRouter) and Gemini Live.
  They are hosted API models — no local weights, no adapter API. Anyone
  claiming to train EV on them is talking about prompting, not training.
- **Trainable AND servable today (zero GPU):** prompt-level personalization —
  the style profile, importance calibration, and filter recalibration. They
  learn from the owner's real ratings and corrections, apply at the prompt and
  scoring layer on every request against the hosted model, and cost nothing to
  train. This is EV's **active** training story.
- **Not available — weight training:** this workspace runs two hosted models
  and no local model, so a locally trained adapter would have **nowhere to
  load**. `run_training` refuses staged weight-training providers rather than
  producing an artifact that cannot be served.

Prompt-level personalization is the whole active training story. The adapter
registry, corpus exporter and eval harness stay in place for any future
provider that can actually serve its output.

## 0.1 HUD surface learning (added)

The window UI is **not a frozen skin**. `eval/hud/surface_corpus.json` is a
one-time public harvest (cited URLs + licenses in
`eval/hud/public_sources.json`; multiple paraphrases per mechanic plus a
held-out slice) and `eval/hud/surface_calibration.json` is the
repo-shipped fit (JARVIS sizes, Karen time-types, E.V.I.E. less-intrusive rule).
This does **not** weight-train the hosted brain. It is a deeper policy fit, scored on
phrases the fit step did not consume.
`app/training/surface.py` scores the live planner against that gold set
(`GET /v1/training/surfaces/smoke`) and folds owner ratings
(`POST /v1/training/surfaces/rate` → `POST /v1/training/surfaces/calibrate`)
into urgency, suppress/boost kinds, and size preference. That calibration is
what `plan_surfaces` reads on every turn.

This is the same class of training as style/importance/filter: evidence in,
policy out, reversible by writing a new version. It is **not**
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
- **Eval harness** (`app/training/eval.py`): held-out split, win-rate vs base,
  tool-call validity, HUD schema conformance, overfitting detection.

**What is not here:** no weight-training runtime (torch, transformers, peft,
accelerate, trl) and no local model. `LocalLoRAProvider` is the generic
configured-command provider.

## 3. Training tracks

| Track | What it learns | Consent track |
| --- | --- | --- |
| Voice enrollment | Speaker voiceprint (enrollment, not weights) | `voice_enrollment` |
| Life-data personalization | Per-domain importance from corrections/usefulness/follow-ignore | `life_data_personalization` |
| Adapter fine-tuning | EV's voice/style/working style from the conversation corpus | `adapter_fine_tuning` |
| Filter self-improvement | Monthly ledger-driven threshold recalibration | `filter_self_improvement` |
| Corpus harvesting | Versioned snapshots feeding the adapter | `training_corpus` |

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

- `local-lora` — runs a configured command (`EV_TRAINING_LOCAL_CMD`).

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
  -d '{"corpus_version": 1, "provider": "local-lora"}'
```


### 7.4 Register

```sh
curl -X POST "$EV_BASE_URL/v1/training/adapter/register" \
  -H "Authorization: Bearer $EV_MASTER_KEY" \
  -d '{"name": "evie-v1", "provider": "local-lora", "corpus_version": 1}'
```

Registration runs the deterministic eval gates; the adapter is `approved` only
if they pass.

### 7.5 Train (configured-command provider only)

`POST /v1/training/adapter/train` with provider `local-lora` runs the command in
`EV_TRAINING_LOCAL_CMD` against the exported JSONL. Staged providers are refused
with a clear explanation instead of producing an artifact with nowhere to load.

```sh
curl -X POST "$EV_BASE_URL/v1/training/adapter/train" \
  -H "Authorization: Bearer $EV_MASTER_KEY" \
  -d '{"corpus_version": 1, "provider": "local-lora",
       "adapter_id": "<adapter-id>", "cost_approved": true}'
```

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

Rollback marks the adapter row rolled back and re-activates the prior version;
the registry row is authoritative.

### 7.8 Erase

```sh
curl -X POST "$EV_BASE_URL/v1/training/adapter/delete" \
  -H "Authorization: Bearer $EV_MASTER_KEY"
curl -X POST "$EV_BASE_URL/v1/training/corpus/delete" \
  -H "Authorization: Bearer $EV_MASTER_KEY"
```

Redacts all snapshots/adapters; exports of redacted snapshots are refused.

## 8. Environment

`EV_TRAINING_PROVIDER` (default `local-lora`) and `EV_TRAINING_LOCAL_CMD` are the
only training settings; see `docs/ENVIRONMENT.md`.

## 10. Verification

```sh
cd backend
uv run pytest tests/test_training_adapter.py tests/test_training_corpus.py \
  tests/test_training_personalization.py tests/test_training_style_profile.py \
  tests/test_training_filter_improvement.py -q
uv run pytest -q
uv run ruff check app clients tests && uv run mypy app clients
```

## 11. REPORT FOOTER (mandatory for every FORGE report)

State which training track ran, with its measured numbers, and say plainly that
weight training is not available in this workspace.
