# EV Models — ML runtime foundation

Owned by Agent 2 (Foundry). This is the install matrix, registry contract, and
device-selection policy for models on the EV host.

## 1. Install matrix

The base install stays tiny and ML-free. Nothing below is installed by default;
CI and offline dev do not pay for ML packages.

| Extra | Packages | Use when |
| --- | --- | --- |
| `ml` | onnxruntime, numpy, tokenizers, datasets, faster-whisper, openwakeword, kokoro-onnx | ONNX models (embedding, VAD, ASR, TTS voices) + voice activation packages |
| `mlx` | mlx, mlx-lm, mlx-tune (Apple Silicon only) | optional trainer only (local LLM inference is not expected) |
| `face` | opencv-python-headless | face/vision pre-processing |
| `dev` | pytest, ruff, mypy | development and CI |

```bash
cd backend
uv sync --extra dev                                  # no ML packages
uv sync --extra ml --extra dev                       # ONNX runtime
uv sync --extra mlx --extra dev                      # Apple Silicon MLX stack
uv sync --extra face --extra dev                     # vision pre-processing
uv sync --extra ml --extra mlx --extra face --extra dev
```

### 1a. Postures

**API-first (recommended)** — reasoning is remote (DeepSeek); only four local
models remain justified and they all come from the `ml` extra:

```bash
uv sync --extra ml --extra dev
```

| Local model | Registry name | Tier | Resident MB |
| --- | --- | --- | ---: |
| Wake word | `wake-openwakeword` | always | 15 |
| Speaker | `speaker-campp` | always | 28 |
| Embeddings | `embed-granite-r2` | on_demand | 460 (measured) |
| Face | `face-sface` | on_demand | 37 |

Apple Vision needs no Python package (native `evvision` helper).

**Measured API-first resident total:** the pinned API-first set is **43 MB**
(wake 15 + CAM++ 28); including VAD/liveness/scene the always-on arbiter
footprint is **64 MB** — well under 300 MB. Caveat: granite R2 measures
**~460 MB resident while loaded** (Agent 8 measurement, 2026-08-11), so a live
embedding session brings the fully-loaded set to ~540 MB; granite rides the
on-demand slot and is evicted when idle. The "<300 MB" expectation therefore
holds for the always-on baseline, not for a concurrent granite session.

**local (optional)** — adds the MLX trainer:

```bash
uv sync --extra mlx --extra dev
```

`mlx` is only for the optional trainer. Local LLM entries (`llm-mlx-3b`,
`qwen3-1.7b`) are registered as **optional and not-expected**: they never
appear in `ml doctor` as missing or required, and the owner may self-host
later. Set `EV_ML_POSTURE=api-first|local` to override auto-detection.

Version pins (researched 2026-08-11):

| Package | Pin | Rationale |
| --- | --- | --- |
| onnxruntime | `>=1.20,<1.29` | CoreML EP is stable on Apple Silicon since 1.17; 1.28 is current stable; 1.18+ removed legacy session APIs, so a modern floor is required. |
| numpy | `>=1.26,<3` | Supported by onnxruntime/opencv; caps at the 2.x line to avoid the NumPy 3 API transition. |
| mlx | `>=0.31,<0.32` | Current 0.31.x toolchain (0.31.2 resolved in uv.lock). |
| mlx-lm | `>=0.31,<0.32` | 0.31.x is the current release line (0.31.3 resolved) used with mlx 0.31. |
| mlx-tune | `>=0.5,<0.6` | Current 0.5.x PyPI line (0.5.1 resolved); pulls mlx-lm >= 0.31. |
| opencv-python-headless | `>=4.10,<5` | Stable 4.x headless wheels; no GUI deps in a server. |

`mlx` extras carry `sys_platform == 'darwin' and platform_machine == 'arm64'`
markers so lock resolution and installs remain sane on non-Apple machines.

## 2. Environment

| Var | Default | Meaning |
| --- | --- | --- |
| `EV_MODEL_DIR` (alias: `EV_ML_MODEL_DIR`) | `~/.ev/models` | Model cache (atomic, resumable downloads) |
| `EV_ML_DATASET_DIR` | `~/.ev/datasets` | Dataset cache |
| `EV_ML_RESIDENT_CEILING_MB` | `2400` | Hard resident ceiling; loads above are refused |
| `EV_ML_ON_DEMAND_SLOT_MB` | `600` | Shared LRU slot for on-demand models |
| `EV_ML_EXCLUSIVE_LIMIT_MB` | `3500` | Registry cap for exclusive tier |
| `EV_ML_MIN_FREE_GB` | `5.0` | Downloads refused below this free-disk floor |
| `EV_ML_POSTURE` | `auto` | `auto` \| `api-first` \| `local`; auto derives from installed extras |

## 3. Registry contract

`backend/app/ml/registry.py` defines `ModelSpec`: `name`, `task`, `source_url`,
`sha256`, `disk_mb`, `resident_mb`, `peak_mb`, `tier`, `license`, `license_url`,
`version`, `verified`.

* A model **without a license cannot be registered** (`LicenseError`).
* An entry with `source_url` but no `sha256` is a seed entry: `pull` refuses
  until a maintainer pins the checksum and marks it `verified`.
* `optional=True` models are loadable but never pinned at boot and never
  reported missing/required by `ml doctor`.
* Downloads are checksum-verified, atomic (final file appears only after the
  hash passes), and resumable via HTTP Range / partial-file continuation.
* Corrupt artifacts are deleted on mismatch.
* `prune` evicts least-recently-used weight files (oldest mtime first).

## 4. Device selection

`backend/app/ml/device.py` picks, in order:

1. **CoreML / ANE** — ONNX Runtime `CoreMLExecutionProvider` on Apple Silicon
2. **MLX / Metal** — `mlx` on Apple Silicon
3. **ONNX Runtime CPU** — portable fallback

CUDA is never assumed. `select_backend()` reports the chosen backend and
reason; `arbiter.stats()["backend"]` exposes it for `/v1/ops` and `doctor`.

## 5. CLI

```bash
python -m app.ml.cli list        # registry table (--json)
python -m app.ml.cli pull NAME   # download + verify (requires pinned sha256)
python -m app.ml.cli verify      # verify cache (NAME or all)
python -m app.ml.cli prune       # LRU eviction (--all, --dry-run)
python -m app.ml.cli stats       # arbiter state as JSON
python -m app.ml.cli doctor      # posture, four-model readiness, backend, ceiling, resident total, free disk
```

## 6. Ops exposure

`ModelArbiter.stats()` is the source for `/v1/ops`: ceiling, resident total,
resident by tier, per-model state, exclusive holder, backend, and free disk.
The ops router can import it without touching this package.

## 7. Voice activation (Agent 2 follow-up)

Run once:

```bash
cd backend
uv sync --extra ml --extra face --extra dev
uv run python -m app.ml.cli pull tts-kokoro-82m-int8 tts-kokoro-voices-v1.0
export EV_VOICE_ASR_PROVIDER=faster_whisper
```

Make targets: `make voice-deps`, `make model-pull-voice`,
`make voice-preflight` (Foundry diagnostics; `make preflight` remains Agent
20's system-wide readiness report). `voice-preflight` prints per-engine
readiness and exact remediation.

### ASR — pragmatic default: faster-whisper

faster-whisper is proven on this machine and is a first-class provider
(`EV_VOICE_ASR_PROVIDER=faster_whisper`; model downloads on first use via
`EV_VOICE_ASR_MODEL`, default `tiny`). Parakeet is **not pinned**: the
streaming split export (`soniqo/Parakeet-EOU-120M-ONNX-INT8`, encoder +
decoder + joint ONNX) does not match `ParakeetOnnxSession`'s single-session
contract, so no sha256 is published until Agent 4 supplies a compatible
export.

### TTS — recommended default: openai_compat

Per fleet law §13, production TTS uses the API seam:
`EV_VOICE_TTS_PROVIDER=openai_compat` with `EV_ALLOW_REMOTE_TTS=true` and
`EV_VOICE_TTS_BASE_URL` / `EV_VOICE_TTS_API_KEY` set. Local offline fallback:
`EV_VOICE_TTS_PROVIDER=kokoro` after pulling the pinned int8 weights (see
above; Apache-2.0, 88 MB model + 27 MB voices).

### Speaker — CAM++ needs Agent 5's export

The verified community ONNX export (`Alkd/campplus-zh-cn-common-200k-onnx`,
Apache-2.0, 27 MB) expects `feats [B,T,80]` fbank input, while
`CamppSpeakerVerifier._embed_onnx` feeds raw 16 kHz waveform. It is therefore
**not pinned** — pinning it would load a model that cannot run. Agent 5 must
either export CAM++ with waveform input (fbank preprocessing inside the graph)
or add fbank preprocessing to the verifier; then `ml pull speaker-campp` can
be pinned. The engine already fails closed with an actionable error when
weights are absent.

### Wake — openwakeword package landed; head owned by Agent 3

`openwakeword>=0.6` is in the `ml` extra. The custom "EVIE" head is trained by
Agent 3 from the owner's `voice-sample/wake/` clips and must land at
`EV_VOICE_WAKE_OPENWAKEWORD_MODEL_PATH` (`~/.ev/models/wake-openwakeword.onnx`
by default). This task does not train or enroll.
