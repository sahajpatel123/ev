# ML eval artifacts — canonical schema registry

Written by `ev-eval` (Agent 2 / Foundry) and read by Agent 20's gates
(`backend/app/scripts/eval_gates.py`) and the regression gate. Artifacts are
never committed: they contain personal audio/photo statistics.

Every artifact is a JSON object carrying:

* `schema` / `schema_version` — version in the table below
* `producer` — `"ev-eval"`
* `generated_at` — ISO-8601 UTC
* the measured keys listed below (unchanged from the owning agent's report)
* `degraded: true` when the run used a deterministic test double (weights
  absent); gates SKIP degraded artifacts instead of treating them as measured

## Schemas

| Artifact | Schema | Measured keys the gates consume | Owning agent entry point |
| --- | --- | --- | --- |
| `retrieval_quality.json` | `ev.retrieval.eval.v1` | `ndcg_at_10`, `top5_hit_rate` (inside `before_after.provider`), `provider`, `degraded` | `eval.retrieval.cli` (Agent 8) |
| `asr_quality.json` | `ev.asr.eval.v1` | `wer_clean`, `wer_owner_speech`, `provider`, `degraded` (Agent 4's harness currently emits `wer_mean`/`wer_samples`; it must add the gate keys before real measurements are reported) | `eval.ml.asr_eval` (Agent 4) |
| `speaker_security.json` | `ev.speaker.eval.v1` | `eer`, `roc` (`[far, tar, threshold]`), `impostor_count`, `degraded` | `python -m app.voice.speaker eval` (Agent 5) |
| `face_recognition.json` | `ev.face.eval.v1` | `tar_held_out`, `stranger_rejection_rate`, `strangers_total`, `strangers_unknown` | `python -m app.people.eval` (Agent 7) |
| `wake_reliability.json` | `ev.wake.eval.v1` | `false_accepts_per_12h`, `recall`, `hours_audio`, `provider`, `degraded` | `python -m app.audio.wake_eval` (Agent 3) |

## Skip semantics

When a measurement's entry point has not landed or its data directories were
not supplied, `ev-eval` writes **no artifact** and prints the exact gate
reason string: `no eval artifact at <path>; <produce hint>`. Agent 20's gates
therefore keep their loud SKIP behavior and no test double is ever presented
as a measured quality number.
