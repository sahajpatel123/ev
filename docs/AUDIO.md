# EVIE Ears — always-on microphone, VAD, wake word, audio scene

Agent 3 (EARS) owns the audio front end: `backend/app/audio/**`,
`backend/app/voice/wake.py`, `backend/clients/ears/**`, `docs/AUDIO.md`, and
the audio test files. This document is the operating manual and privacy
contract for that stack.

## 1. Why it exists

Before EARS, nothing in EV ever opened a microphone. The "always-on ear" was a
heartbeat HTTP client that received utterance text as a command-line flag, and
the wake engines were string matchers. EARS replaces that with a real capture
pipeline so EVIE can actually hear:

```
microphone (16 kHz mono int16)
  → lock-free PCM16 ring (≥ 10 s pre-roll)
  → streaming VAD (Silero v5 ONNX, energy/ZCR double)
  → wake word (custom "EVIE" openWakeWord head + speaker verifier)
  → audio scene (YAMNet ONNX, VAD-feature double)
  → [explicit consent] wake-passing utterance to Agent 4 (/v1/ears/wake)
```

## 2. Components

### `app/audio/ring.py`
`PCM16RingBuffer` — fixed-capacity, single-producer/single-consumer pre-roll
ring. Capacity defaults to ≥ 10 s at 16 kHz (160 000 samples, ~320 KB). The
writer overwrites the oldest samples when full, so the most recent 10 s are
always available when VAD detects the start of an utterance.

### `app/audio/capture.py`
`MicrophoneStream` — 16 kHz mono int16 PortAudio stream via `sounddevice`.
Choice: sounddevice/PortAudio over a Swift CoreAudio helper because it is a
single maintained Python wheel with static PortAudio, works on Apple Silicon,
and keeps the whole pipeline testable in-process. If PortAudio proves unstable
on a future macOS release, the `MicrophoneStream` seam lets us swap in a tiny
CoreAudio helper without touching the ring/VAD/wake layers.

Permission handling is loud: a denied macOS Microphone (TCC) permission raises
`MicrophoneDeniedError` with exact remediation text and the ears process exits
with a clear message — it never runs silently on zero audio.

### `app/audio/vad.py`
- `SileroVadOnnx` — Silero VAD v5 ONNX (2 MB, MIT) at 16 kHz, loaded through
  the ModelArbiter. Streaming `block_probability()` buffers to 512-sample
  frames; `frame_probabilities()` scores an offline buffer.
- `EnergyVad` — the existing energy/ZCR heuristic as the deterministic
  zero-dependency double.
- `StreamingSegmenter` — incremental utterance builder with configurable
  pre-roll (0.25 s), post-roll (0.75 s), and minimum speech length (0.2 s).

### `app/voice/wake.py`
- `PhraseWakeEngine` — dev/test string matcher only.
- `PorcupineWakeEngine` — real engine; **never** delegates to the string
  matcher when a `text_hint` is present (the old short-circuit is removed).
- `OpenWakeWordEngine` — the custom "EVIE" head. It wraps openWakeWord's
  documented inference: `Model(wakeword_models=[head.onnx])` plus
  `custom_verifier_models={"evie": verifier.pkl}` when the verifier exists.
- `default_wake_engine()` supports `phrase | porcupine | silero_vad |
  openwakeword`, selected by `EV_VOICE_WAKE_PROVIDER`.

### `app/audio/scene.py`
- `YamNetSceneClassifier` — YAMNet ONNX (17 MB, Apache-2.0) mapping the
  AudioSet 521-class output to EV's five classes (`speech`, `meeting`,
  `music`, `noise`, `silence`). Label-driven mapping when a class-map CSV is
  configured; fallback indices: Speech 0–10, Conversation/Narration 3–4,
  Music 132, Silence 494.
- `classify_wav()` — the public interface. Uses YAMNet when configured and
  loadable, otherwise degrades to `vad_features` with `degraded: true`.
- `_tone_score()` is vectorized with numpy when available; the pure-Python
  loop remains the no-numpy fallback.

### `app/audio/diarize.py`
Optional, on-demand pyannote 3.1 for meeting recordings **only**. Never
resident, never live ambient; gated behind explicit consent
(`EV_EARS_DIARIZE_CONSENT=true` per selected recording).

### `backend/clients/ears/`
Standalone always-on process (`python -m clients.ears`). Flags:

```
--device / --list-devices
--sample-rate / --ring-seconds / --block-ms
--vad-model-path / --vad-threshold / --vad-pre-roll-s / --vad-post-roll-s
--wake-model-path / --wake-verifier-path / --wake-threshold
--scene-model-path / --scene-labels-path
--api-url / --api-key / --consent / --dry-run
--save-segments-dir            # explicit opt-in debug dump (wake-passing only)
--duration SECONDS             # bounded run for verification
--report-interval-s            # RSS / avg CPU / ring-fill log
```

Runtime contract: ≤ 60 MB RSS, bounded ring, no unbounded buffers, graceful
shutdown on SIGINT/SIGTERM, retry-and-exit on repeated capture errors. Average
CPU is reported every `--report-interval-s` (default 300 s).

## 3. Training the custom wake engine

1. `python -m app.audio.capture_eval` — guided wizard: records the 30 "EVIE"
   clips (10 at ~3 m), 10 non-wake negatives, and a long ambient session
   (recorded in chunks, or ingested with `--ingest-ambient PATH`). It prints
   exactly what is still missing when it finishes — no docs needed.
2. (Optional synthetic positives) `python -m clients.ears.train.synthesize` — piper-sample-generator
   positives, RIR + background-noise augmentation.
3. `python -m clients.ears.train.train_head` — openWakeWord's official trainer
   on the frozen shared feature extractor; exports `evie.onnx`.
4. `python -m clients.ears.train.train_verifier` — logistic regression on the
   human's own clips via `openwakeword.train_custom_verifier` (the documented
   false-accept crusher).
5. `python -m app.audio.wake_eval --held-out-dir backend/data/wake/clips
   --ambient backend/data/wake/ambient --model-path <evie.onnx>
   --verifier-path <verifier.pkl>` — measures false accepts/12 h and recall,
   sweeps the threshold, records the distance breakdown and verifier
   before/after, and writes `backend/eval/ml/wake_reliability.json` (the
   artifact Agent 2's `ev-eval wake` and Agent 20's `wake_reliability` gate
   consume). The ambient session is replayed faster than real time and the
   measured `replay_speed_x` is reported.
6. `python -m clients.ears.train.tune_threshold` — same sweep, writing the
   tuned value to `data/wake/tuned-threshold.json`.
7. `python -m clients.ears.train.evaluate` — VAD frame accuracy and scene
   top-1/confusion gates.

### 3a. Threshold, patience, and the wake reliability artifact

The runtime threshold is the sensitivity/patience dial for wake: a higher
`EV_VOICE_WAKE_OPENWAKEWORD_THRESHOLD` / `EV_EARS_WAKE_THRESHOLD` accepts fewer
false wakes at the cost of recall. `wake_eval` sweeps the threshold against
the owner's real ambient audio, publishes the full false-accept/recall curve
in the artifact (`threshold_curve`), and prints the shipped value — never a
default. Utterance patience is separate: `EV_EARS_VAD_POST_ROLL_S` controls
how long the ears process keeps a segment open after speech ends, and
`EV_EARS_MAX_SEGMENT_S` caps segment length so memory stays bounded even
during continuous speech.

Artifact schema (`ev.wake.eval.v1`): `provider`, `degraded`,
`false_accepts_per_12h`, `recall`, `hours_audio`, `threshold`,
`threshold_curve`, `distance_breakdown`, and — when a verifier is configured —
`verifier` with head-only vs with-verifier false-accept/recall numbers.

### 3a-1. Owner wake training (exact paths)

The trained artifacts go where `.env` already expects them:

* `~/.ev/models/wake-openwakeword.onnx` (`EV_VOICE_WAKE_OPENWAKEWORD_MODEL_PATH`)
* `~/.ev/models/wake-openwakeword-verifier.pkl`
  (`EV_VOICE_WAKE_OPENWAKEWORD_VERIFIER_PATH`)

Both are expanded with `~` → the owner's home directory before the engine
opens them. The target wake phrase is **"EVIE" (E-V-I-E)**, not "Eve".

Existing recordings (m4a/wav/…) can be ingested into the CapturePlan layout
without a microphone:

```
python -m app.audio.capture_eval \
  --ingest-clips <wake clips dir or file> \
  --ingest-negatives <non-wake speech dir or file> \
  --ingest-ambient <ambient dir or file> \
  --ingest-only
```

Files whose names contain `3m`/`far` are tagged as far clips; everything else
is tagged close. Non-16 kHz mono audio is converted with `ffmpeg` (or
`afconvert`) to 16 kHz mono PCM16 WAV.

Current owner-data status: `voice-sample/` contains one EVIE clip (44.6 s),
one non-wake paragraph, and five enrollment samples (Agent 5's — never used
for wake training). The required 30 clips (10 at 3 m) and ambient recording
are not present yet, so training stops with the exact shortfall instead of
faking a model.

If ambient is unavailable at tuning time, the threshold is shipped
conservative (higher than the 0.5 default is the safe direction) and explicitly
marked unmeasured until the owner ambient session exists.

### 3b. Long-run resource measurement

`python -m clients.ears --simulate-wav <16 kHz mono WAV> --resource-report
backend/data/wake/ears_resources.json --report-interval-s 60 --duration 3600` runs the
full ears loop (VAD → segmentation → wake) at real-time pacing against an
offline WAV, so RSS/CPU can be measured without a microphone. At shutdown it
writes `rss_max_mb`, `avg_cpu_fraction`, wall time, and the bounded-buffer
limits. Requirements: ≤ 60 MB RSS, ≤ 3% average CPU, no unbounded growth.

## 4. Model budget (locked roster)

| Model | Arbiter name | Tier | Resident |
| --- | --- | --- | ---: |
| Custom EVIE head | `wake-evie-porcupine` | always | 16 MB |
| Silero VAD v5 | `vad-silero` | always | 2 MB |
| YAMNet | `scene-yamnet` | always | 17 MB |

All loads go through `app/audio/models.py` → ModelArbiter. Always-resident
ears total: 35 MB. The ears process target is ≤ 60 MB RSS including Python.

## 5. Privacy contract

- Raw audio is **never persisted by default**. The ring lives in RAM only.
- Only VAD-segmented utterances that pass the wake engine are eligible to
  leave the device, and only when `EV_EARS_CONSENT=true` **and**
  `EV_EARS_API_URL` is set; they go to `/v1/ears/wake` as base64 PCM.
- Only derived scene labels are stored by the pipeline.
- `EV_EARS_SAVE_SEGMENTS_DIR` is an explicit opt-in debug dump; it writes only
  wake-passing segments as WAV and must not be set in normal operation.
- Diarization requires a separate, per-recording consent flag.
- No ambient raw media is ever sent to any model.

## 6. Required human approvals (data gates)

The acceptance metrics cannot be proven without owner-consented data:

| Data | Purpose | Gate |
| --- | --- | --- |
| Microphone permission (macOS TCC) | live capture | — |
| 30 "EVIE" clips (10 at 3 m) | wake head/verifier training | recall ≥ 90% |
| 12 h ambient recording | threshold tuning | ≤ 1 FA / 12 h |
| 20 hand-labeled VAD clips | VAD accuracy | ≥ 95% frames |
| 100 labeled scene clips (20/class) | scene accuracy | ≥ 80% top-1 |

## 7. Dependency requests (Agent 2)

| Package | Why | Size |
| --- | --- | --- |
| `sounddevice` | PortAudio mic capture (ears process) | ~1.5 MB wheel |
| `openwakeword` | custom EVIE head inference + verifier | ~15 MB |
| `openwakeword` (train) | custom head training/ONNX export | same |
| `piper-sample-generator` + a Piper voice | synthetic positives | ~65 MB |
| `pyannote.audio` | optional meeting diarization (on-demand) | ~50 MB |
| ml extra (`numpy`, `onnxruntime`) | Silero/YAMNet/head ONNX runtime | already declared |

Until Agent 2 lands them, every import is lazy and the stack degrades to the
deterministic doubles; offline CI stays green.

## 8. Hands-free wake (Vosk) and `/v1/ears/wake`

The production always-on path is **not** the ears process posting blobs. It is
`WS /v1/voice/live`: the server runs a grammar-restricted Vosk spotter on the
continuous mic stream (see `docs/VOICE.md` §13). The ears process remains the
on-device VAD/scene pipeline and still POSTs wake-passing segments to
`POST /v1/ears/wake` when `EV_EARS_CONSENT=true`.

That route is now wired. Agent 4 opens a hands-free session (no spoken
challenge) and runs the utterance. When the Vosk model is on disk
(`~/.ev/models/vosk-model-small-en-us-0.15`) the ears process uses
`VoskWakeEngine` instead of the ASCII `PhraseFallbackWake` double — real
speech never contains the bytes `b"evie"`.

Preferred clients for talking to EVIE:

* Web workbench `/app` (Hands-free panel)
* macOS menu bar (Hands-free toggle)
* `python -m clients.hands_free`

Install models with `uv run python -m app.voice.models_setup`.
