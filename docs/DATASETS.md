# EV Datasets — registry, licenses, eval-only law

Owned by Agent 2 (Foundry). Only public legal datasets and owner-consented
personal data are allowed (fleet law).

## 1. Registry contract

`backend/app/datasets/registry.py` defines `DatasetSpec`: `name`,
`description`, `source_url`, `sha256`, `bytes`, `eval_only`, `streaming`,
`license`, `license_url`, `verified`.

* A dataset **without a license cannot be registered** (`DatasetLicenseError`).
* Seed manifests are metadata only: they carry `source_url` but an unpinned
  `sha256`; `pull` refuses until the checksum is pinned and verified.
* `eval_only=True` is enforced in code: any consumer must pass
  `eval_context=True` (via `guard_eval` / `use_dataset`), otherwise
  `DatasetEvalOnlyError` is raised **before** any download.
* Downloads are atomic, resumable, and checksum-verified; corrupt artifacts are
  removed.

## 2. Seed manifests

| Name | Content | Approx bytes | License | Eval-only |
| --- | --- | --- | ---: | --- |
| librispeech_test_clean | LibriSpeech test-clean subset (~5.4 h) | 346 MB | CC BY 4.0 | yes |
| voxceleb1_o_cleaned_trials | VoxCeleb1-O cleaned trial pairs | ~1.1 GB | VoxCeleb research (non-commercial) | yes |
| esc50 | 2,000 environmental audio clips | ~662 MB | **CC BY-NC 4.0** | yes |
| lfw | 13,233 face images | ~233 MB | LFW non-commercial research | yes |
| audioset_balanced_eval | AudioSet balanced eval segment CSV | ~26 MB | CC BY 4.0 (annotations) | yes |

All seed manifests are evaluation datasets and therefore `eval_only`. ESC-50's
CC BY-NC license is marked in code and cannot be used outside an eval context.

## 3. Lifecycle

* `pull` streams in chunks (no full-buffer download) and writes to
  `EV_ML_DATASET_DIR` (default `~/.ev/datasets`).
* `use_dataset(..., delete_after_use=True)` removes the artifact when the
  context exits — the default.
* `prune` evicts least-recently-used artifacts (`--all`, `--dry-run`).

## 4. CLI

```bash
python -m app.datasets.cli list          # table (--json)
python -m app.datasets.cli pull NAME     # download + verify (--stream)
python -m app.datasets.cli verify NAME   # verify one or all
python -m app.datasets.cli prune         # LRU eviction (--all, --dry-run)
```

## 5. Using a dataset in code

```python
from app.datasets import store
from app.datasets.registry import DatasetRegistry, builtin_datasets

reg = DatasetRegistry()
for spec in builtin_datasets():
    reg.register(spec)

with store.use_dataset(
    "esc50", reg, settings, eval_context=True, delete_after_use=True
) as path:
    ...  # evaluate on the local artifact; it is removed afterwards
```
