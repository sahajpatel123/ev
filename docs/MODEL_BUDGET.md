# EV ML Model Budget — binding law for Agents 3–16

**Owner:** Agent 2 (Foundry). **Applies to:** every agent that loads a model,
downloads weights, or reserves memory on the host (Apple M2 · 8 GB unified ·
shared with macOS, Postgres, Redis, and four Python services).

The arbiter in `backend/app/ml/arbiter.py` enforces this document at runtime.
Before asking Foundry for a model, run:

```bash
cd backend && uv run python -m app.ml.cli stats
```

and self-check that your requested `resident_mb` fits the rules below.

---

## 1. Hard numbers (do not negotiate)

| Budget | Value | Env var |
| --- | --- | --- |
| Resident ceiling (hard) | **2400 MB** | `EV_ML_RESIDENT_CEILING_MB` |
| Always/system pin budget (expected) | **64 MB** | — |
| On-demand shared slot | **600 MB** | `EV_ML_ON_DEMAND_SLOT_MB` |
| Exclusive tier registry cap | 3500 MB | `EV_ML_EXCLUSIVE_LIMIT_MB` |
| Disk guard (refuse downloads below) | **5 GB free** | `EV_ML_MIN_FREE_GB` |
| Model cache | `~/.ev/models` | `EV_MODEL_DIR` (alias: `EV_ML_MODEL_DIR`) |

A load that would push resident total above the ceiling is **refused**, never
silently swapped. An exclusive model whose `resident_mb` exceeds the remaining
headroom under the ceiling is refused too, even if it is under the 3500 MB
registry cap.

## 2. Tier rules

| Tier | Rule |
| --- | --- |
| `always` | Pinned at boot; never evicted. Expected total is 64 MB (43 MB API-first set + VAD/liveness/scene). Optional entries are never pinned at boot. |
| `system` | Pinned on first use; never silently evicted. |
| `on_demand` | Shares one ~600 MB slot; LRU eviction only among inactive models. A single model larger than the slot is refused. |
| `exclusive` | Evicts all on-demand models, takes a global lock, and blocks other loads until evicted/released. Its only remaining real consumer is the **optional trainer**; local LLM entries are optional and never expected. |

## 3. Locked roster

| Model | Task | Tier | Resident MB | Disk MB | Peak MB | License | Optional |
| --- | --- | --- | ---: | ---: | ---: | --- | --- |
| embed-granite-r2 | embedding (API-first) | on_demand | 460 | 100 | 520 | Apache-2.0 | no |
| embed-all-minilm-l6-v2 | embedding (legacy) | always | 100 | 95 | 100 | Apache-2.0 | yes |
| wake-openwakeword | wake word (API-first) | always | 15 | 15 | 20 | Apache-2.0 | no |
| wake-evie-porcupine | wake word (legacy) | always | 16 | 12 | 16 | Apache-2.0 (access key) | yes |
| vad-silero | VAD | always | 2 | 2 | 2 | MIT | no |
| speaker-campp | speaker embedding (API-first) | always | 28 | 30 | 40 | Apache-2.0 | no |
| speaker-ecapa | speaker embedding (legacy) | always | 28 | 60 | 28 | Apache-2.0 | yes |
| liveness-audio | liveness | always | 2 | 2 | 2 | MIT | no |
| scene-yamnet | audio scene | always | 17 | 20 | 17 | Apache-2.0 | no |
| asr-faster-whisper-tiny | ASR | on_demand | 75 | 80 | 75 | MIT | no |
| asr-faster-whisper-base | ASR | on_demand | 145 | 150 | 145 | MIT | no |
| tts-piper-en-lessac-medium | TTS | on_demand | 60 | 65 | 60 | MIT | no |
| face-sface | face embedding (API-first) | on_demand | 37 | 37 | 90 | Apache-2.0 | no |
| llm-mlx-3b | local LLM | exclusive | 2000 | 2200 | 2400 | Llama 3.2 Community | yes |
| qwen3-1.7b | local LLM (CORTEX offline brain) | exclusive | 1000 | 1100 | 1200 | Apache-2.0 (Qwen3) | yes |
| trainer-mlx-lora | trainer | exclusive | 2000 | 2400 | 3500 | Llama 3.2 Community | yes |

Expected always total (non-optional): **64 MB** = wake-openwakeword 15 +
speaker-campp 28 + vad-silero 2 + liveness-audio 2 + scene-yamnet 17.
The API-first pinned set alone is **43 MB** (wake 15 + CAM++ 28). On-demand
models never exceed the 600 MB slot; granite R2 measures ~460 MB resident
while loaded, so a live embedding session pushes total to ~540 MB — the
2400 MB ceiling still allows it, but the on-demand slot keeps it evictable.
Exclusive models fit under the ceiling when used (64 + 2000 = 2064 MB), and
are **optional**: local LLM inference is not expected on this machine
(reasoning is DeepSeek API); the only remaining real exclusive consumer is the
optional trainer.

## 4. Agent allocations

| Agent | Owned models | Budget check |
| --- | --- | --- |
| 3 — Voice | wake-openwakeword 15 · vad-silero 2 · asr-* on_demand · tts-piper on_demand | 17 MB pinned + one on_demand model in the 600 MB slot |
| 4 — Runtime | liveness-audio 2 | 2 MB pinned |
| 5 — Voice security | speaker-campp 28 | 28 MB pinned |
| 7 — Face | face-sface 37 | on_demand in the 600 MB slot |
| 8 — Gateway & Tools | embed-granite-r2 460 on_demand · llm-mlx-3b exclusive 2000 (optional) | on_demand slot; LLM only via exclusive lock |
| 10 — CORTEX | qwen3-1.7b exclusive 1000 (optional, Ollama-managed) | exclusive lock; evicts on-demand models |
| 11 — Perception | scene-yamnet 17 | 17 MB pinned |
| 12 — Training | trainer-mlx-lora exclusive 2000 (optional) | exclusive lock; evicts all on-demand |
| 3–16 (all) | shared | run `stats` and verify `resident_total_mb <= 2400` before asking for anything |

## 5. How to request a model

1. Find the registry name in `backend/app/ml/registry.py` (`list`).
2. Confirm your total (your pinned models + your on-demand/exclusive request)
   stays under the ceiling.
3. Call `arbiter.acquire(name)` before loading real weights; never load
   outside the arbiter.
4. `exclusive` callers: use `release_on_exit=True` or explicit `evict()` so the
   global lock does not starve voice/perception.
5. Downloads: `python -m app.ml.cli pull NAME` only after the entry's sha256 is
   pinned and `verified=True`; seed entries refuse until then.

## 6. Disk law

No download starts when free disk is below `EV_ML_MIN_FREE_GB` (default 5 GB).
To reclaim space: `python -m app.ml.cli prune` (LRU order, oldest first) or
`prune --all`. Corrupt artifacts are removed automatically on checksum failure.
