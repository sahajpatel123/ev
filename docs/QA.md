# EV — Ops & Launch QA

**Version 2.0 (LAUNCH)** — evidence that the EV stack is shippable on this
Mac: gates are truthful, the native stack boots, backups restore, disk is
recoverable, and the go-live runbook was executed with failures documented.

## 1. Gate truthfulness (the headline fix)

Before LAUNCH, none of the 12 eval gates measured model quality. LAUNCH adds
six ML gates (docs/EVALUATION.md §12) reading artifacts from
`backend/eval/ml/`:

| Gate | Offline CI behavior | Measured artifact behavior |
| --- | --- | --- |
| `asr_quality` | SKIP (no artifact / degraded) | FAIL on WER > 8% clean / > 12% owner |
| `speaker_security` | SKIP (no artifact / degraded) | FAIL on EER > 3% or any false accept |
| `retrieval_quality` | SKIP (no artifact / degraded) | FAIL on nDCG@10 < 0.80 or top-5 < 90% |
| `face_recognition` | SKIP (no artifact / degraded) | FAIL on TAR < 95% or stranger rejection < 100% |
| `wake_reliability` | SKIP (no artifact / degraded) | FAIL on > 1 false accept/12 h or recall < 90% |
| `grounding` | measured in-process | FAIL on recall < 95% or false removal > 5% |

`regression` now catches ML metric degradation, not just latency. A test
double is never reported as a quality number.

## 2. Verified commands (2026-08-11/12)

| Command | Result |
| --- | --- |
| `uv run pytest tests/test_eval_gates.py tests/test_backup.py tests/test_ops_metrics.py tests/test_compliance_regional.py tests/test_maintenance.py -q` | 52 passed |
| `uv run python -m app.scripts.eval_gates --report eval/last-run.json` | 18/18 gates exit 0; 5 ML gates skip with explicit reasons, grounding measured |
| `uv run pytest -q` | full suite (see runbook log) |
| `uv run ruff check app clients tests` + `uv run mypy app clients` | 0 errors |
| `make prune-dry-run` | 146.7 MB would be freed (dry run) |
| `make doctor` | one-screen system/API diagnosis |

## 3. Native stack

`brew/setup.sh` installs PostgreSQL 17 + pgvector + Redis and loads the
launchd EV services plus the nightly `ev.backup` job. Compose is CI-only;
MinIO is out of the daily path (object store default `local`). Deployment
gate asserts native-primary topology with one-line justifications on every
changed assertion (docs/DEPLOYMENT.md §2–3).

## 4. Backup/restore

Encrypted snapshots (Fernet + scrypt passphrase) exclude model/dataset caches
by construction; wipe→restore drill verifies counts, attachment blobs, and a
sample audit trail. `restore_drill.age_days` is published in `/v1/ops/metrics`
with a stale alert past 35 days.

## 5. Residual risk

See docs/OPS.md §8. The short version: 8 GB RAM is the wall, five ML gates
still skip until the owning agents produce real artifacts, and the backup
passphrase ceremony is manual.
