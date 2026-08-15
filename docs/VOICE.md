# EVIE Voice & Speech — Operator Guide

The voice lifecycle (`wake → verify → listen → process → respond → follow-up →
idle`) is provider-agnostic. The runtime depends only on the protocols in
`backend/app/voice/contracts.py`; real engines are config-driven swaps behind
that contract, and offline CI always runs on the deterministic dev doubles.

This document covers the **real on-device engines** (Parakeet ASR, Kokoro TTS),
the fail-closed/degradation rules, streaming, audio persistence, barge-in, and
exactly what leaves the device under which flag. Since the host cannot run a
local LLM, **API-first is a first-class production configuration**: hosted
OpenAI-compatible ASR/TTS are recommended when a valid key exists, and local
Parakeet/Kokoro remain the no-network, no-cost default once weights are
registered.

The HTTP utterance path in this document is the **turn-based** door
(`POST /v1/voice/utterance` / SSE). The continuous full-duplex runtime —
turn-taking, thinking pauses, backchannels, native barge-in, and
foreground conversation + background DeepSeek — lives in
[`LIVE_VOICE.md`](LIVE_VOICE.md) and `WS /v1/voice/live`. Both share the
same ASR/TTS contracts, wake session, and chat pipeline.

The spoken brain for **typed chat and the HTTP utterance path** is still a
chat-completions model: official DeepSeek (`deepseek-v4-flash`) or official
xAI (`grok-4.6`) via `EV_CHAT_PROVIDER`.

**Grok Voice Think Fast 2.0 is not that.** It is a speech-to-speech realtime
model (`wss://api.x.ai/v1/realtime`). When `EV_XAI_API_KEY` is set and
`EV_VOICE_LIVE_BRAIN` is `auto` or `xai`, `WS /v1/voice/live` forwards 16 kHz
PCM to that model and plays its audio back as `tts_chunk` events. Typed chat
can stay on DeepSeek — live does not send every question through
chat-completions. Local ASR/TTS/turn-taking step aside for that channel. EV
life tools still run here when Grok asks. Until the xAI key is set, live
keeps the DeepSeek pipeline.

Thinking/CoT is off for DeepSeek (`EV_DEEPSEEK_THINKING=false`) and Grok Voice
uses `reasoning.effort=none` so the first spoken audio is the answer.
Actions the owner asked for still run in EV (`backend/app/ev/turn.py` on the
pipeline path; function calls on the Grok Voice path). `opencode-go` remains
an optional fallback, not the voice default.

## 0. Session state machine (Wave Life)

The wake word is a **door for when EV.app is not already listening**, not a
password for every sentence. Opening EV.app starts the full-duplex live
channel immediately (`POST /v1/voice/live/open` + `WS /v1/voice/live`); you
do not say EVIE. When the app is closed, one wake from `ev.ears` opens a
verified session; the owner stays in conversation until a clear dismissal or
long true idle.

```text
IDLE ──wake "EVIE"──▶ VERIFY (once) ──owner match──▶ AWAKE (ACTIVE)
                                                       │
                                          utterance ──▶ PROCESSING ──▶ RESPONDING
                                                                       │
                                                          reply ──────▶ FOLLOW_UP
                                                                       │
                          owner utterance resets timer ◀───────────────┘
                                                       │
      sleep phrase / explicit end / long idle ────────▶ ENDED (IDLE)
```

States:

| State | Meaning |
| --- | --- |
| `idle` | Listening for the wake word only. |
| `verifying` | Wake accepted; one-time owner verification is required. |
| `awake` | Active conversation; no re-wake is needed. |
| `processing` / `responding` | Utterance → chat/TTS pipeline is running. |
| `follow_up` | Short hands-free window; every owner-verified utterance resets it. |
| `ended` | Sleep phrase, explicit end, silence-lock, verify failure, or replay rejection. |

### Follow-up window

`EV_VOICE_FOLLOW_UP_SECONDS` (default `240`, recommended range 180–300) is the
short REST hint shown as `follow_up_remaining_seconds` after a reply. It is
**not** a session door: when the hint elapses the session stays listening
(`awake` / `follow_up`) until a sleep phrase, an explicit end, or the long
idle lock (`EV_VOICE_SESSION_TIMEOUT_SECONDS`, default `900`). Every accepted
owner turn resets both the hint and the idle lock. The always-on ears path
and a second Talk/PTT press reuse the open session — you do not say EVIE
again. Ambient clips during that session are ignored; they do not close the
door or extend the idle lock. If chat/TTS is already running (`processing` /
`responding`), overlapping mic clips return `still listening` instead of
HTTP 409. A session stuck in `processing` for more than 60 seconds is
treated as crashed and recovered in place.

### Sleep phrases

These clear dismissals end the session without sending the phrase to chat:

- `that's all`
- `that's it`
- `go to sleep`
- `stop listening`
- `stop EVIE`
- `goodbye EVIE`
- `never mind`

The phrase list is configurable via `EV_VOICE_SLEEP_PHRASES` (JSON array).

### Addressivity — who is talking

During `awake` / `follow_up`, every **audio** utterance runs VAD (Silero ONNX
when configured, energy double otherwise) and then owner speaker verification
against the current enrollment. Speech that fails either check is ignored
(`403 voice_ignored`); it is **never** sent to ASR or chat. Ambient TV and other
people cannot steal the session. Explicit push-to-talk (`push_to_talk: true` in
`POST /v1/voice/utterance`) bypasses both checks for button-held capture.
Text-only requests remain the owner-authenticated dev/test surface.

### One lifelong thread

Every utterance resolves to the one default conversation thread
(`EV_VOICE_CONTINUITY_CONVERSATION_ID`, default `CONTINUITY_LIVE`). Waking again
does not create a new chat; the pipeline reuses the same default thread, so
history, rollup, and working state persist across wakes (see
`docs/CONTINUITY_LIVE.md`).

## 1. Engine matrix

| Layer | Dev/test double (default config) | Real on-device engine | Opt-in real engines |
| --- | --- | --- | --- |
| ASR | `echo` — returns the supplied hint with confidence 0.0, refuses audio | `openai_compat` (hosted, API-first) **or** `parakeet` — Parakeet-EOU-120M INT8 ONNX streaming (local) | `parakeet_tdt` (TDT-v3 accuracy tier); `faster_whisper` (legacy) |
| TTS | `meta` — SSML prosody metadata, no audio | `openai_compat` (hosted, API-first) **or** `kokoro` — Kokoro-82M INT8 ONNX, 54 voices, Apache-2.0 (local) | `chatterbox` (Chatterbox-Nano expressive/cloned voice); `piper` (legacy) |
| Offline re-transcription | — | — | `qwen3-asr-0.6b-mlx` batch pass (never resident) |

### Budgets (docs/MODEL_BUDGET.md)

| Model | Tier | Resident | Disk | License |
| --- | --- | ---: | ---: | --- |
| `asr-parakeet-eou-120m-int8` | on_demand | ~230 MB | ~232 MB | CC-BY-4.0 (NVIDIA Parakeet) |
| `asr-parakeet-tdt-v3-int8` | on_demand | per Foundry sizing | per Foundry sizing | CC-BY-4.0 (NVIDIA Parakeet) |
| `tts-kokoro-82m-int8` | on_demand | ~80 MB | ~82 MB | Apache-2.0 |
| `tts-chatterbox-nano` | on_demand | ~110 MB | ~110 MB | MIT (verify at pull) |
| `asr-qwen3-0.6b-mlx` | never resident (subprocess only) | 0 | ~1 GB | Apache-2.0 (Qwen3) |

ASR and TTS share the ~600 MB on-demand slot and are evicted LRU; they are
never resident at the same time as the exclusive local LLM. **Registry entries
are owned by Agent 2 (Foundry)** — see §9 dependency note.

## 2. Install & weight pull

Runtime dependencies are lazy: the suite stays green with zero weights and no
optional packages installed.

```bash
cd backend
# Optional runtime deps (Agent 2 must land these in pyproject.toml first):
#   ml extra: onnxruntime, numpy
#   kokoro:   kokoro (+ its phonemizer/misaki deps)
#   chatterbox: chatterbox
#   mlx:      mlx, mlx-lm (Apple Silicon only)

# Weights (registry owned by Agent 2; commands assume entries exist):
uv run python -m app.ml.cli pull asr-parakeet-eou-120m-int8
uv run python -m app.ml.cli pull tts-kokoro-82m-int8
uv run python -m app.ml.cli pull tts-kokoro-82m-int8.voices   # if a separate artifact
```

The engines look for artifacts under `EV_ML_MODEL_DIR` (default
`~/.ev/models/`) with the canonical registry names, or at explicit
`EV_VOICE_ASR_ONNX_PATH` / `EV_VOICE_TTS_MODEL_DIR` paths. Parakeet also needs
the sibling `<model>.vocab.json` tokenizer map; Kokoro needs the voices pack
(`<model>.voices.bin`) for the 54-voice pack.

## 3. Environment matrix

| Key | Default | Values | Notes |
| --- | --- | --- | --- |
| `EV_VOICE_ASR_PROVIDER` | `echo` | `echo` / `parakeet` / `parakeet_tdt` / `faster_whisper` / `openai_compat` | `echo` is dev/test only and refuses audio |
| `EV_VOICE_ASR_ENGINE` | `parakeet-eou-120m-int8` | registry name | Canonical engine for `parakeet` |
| `EV_VOICE_ASR_ALT_ENGINE` | `parakeet-tdt-v3-int8` | registry name | Engine for `parakeet_tdt` |
| `EV_VOICE_ASR_ONNX_PATH` | — | path | Explicit `.onnx`; overrides cache resolution |
| `EV_VOICE_ASR_VAD_FILTER` | `true` | boolean | Wired: gates Parakeet segmentation (and faster-whisper) |
| `EV_VOICE_ASR_STREAM_CHUNK_MS` | `200` | ms | Streaming partial cadence (first partial budget: <=300 ms) |
| `EV_VOICE_ASR_ALLOWED_ROOTS` | — | JSON list | Extra allowlisted dirs for `audio_ref` paths |
| `EV_VOICE_TTS_PROVIDER` | `meta` | `meta` / `kokoro` / `chatterbox` / `piper` / `openai_compat` | `meta` is dev/test only |
| `EV_VOICE_TTS_ENGINE` | `kokoro-82m-int8` | registry name | Canonical engine for `kokoro` |
| `EV_VOICE_TTS_KOKORO_VOICE` | `af_heart` | one of 54 Kokoro voices | e.g. `am_michael`, `bf_emma` |
| `EV_VOICE_CHATTERBOX_ENGINE` | `chatterbox-nano` | registry name | Engine for `chatterbox` |
| `EV_VOICE_CHATTERBOX_VOICE` | `default` | speaker id | Cloned-voice reference |
| `EV_ALLOW_REMOTE_ASR` | `false` | boolean | Must be true for `openai_compat` ASR |
| `EV_ALLOW_REMOTE_TTS` | `false` | boolean | Must be true for `openai_compat` TTS |
| `EV_VOICE_FOLLOW_UP_SECONDS` | `240` | int | REST follow-up hint (recommended 180–300); every owner utterance resets it. Not a session door. |
| `EV_VOICE_SESSION_TIMEOUT_SECONDS` | `900` | int | Long-idle lock; must exceed the follow-up window |
| `EV_VOICE_VERIFY_TIMEOUT_SECONDS` | `20` | int | One-time verification window after wake |
| `EV_VOICE_CONTINUITY_CONVERSATION_ID` | `CONTINUITY_LIVE` | string | Canonical name of the one lifelong conversation thread |
| `EV_VOICE_ADDRESSIVITY_ENABLED` | `true` | boolean | Per-utterance VAD + owner-verification gate |
| `EV_VOICE_ADDRESSIVITY_VAD_THRESHOLD` | `0.5` | float | Minimum mean VAD probability before owner verification |
| `EV_VOICE_SLEEP_PHRASES` | JSON list | JSON array | Dismissal phrases that end the session without chat |

Both remote gates run through `compliance.policy.remote_processing_allowed`
(tracks `voice_asr` and `voice_tts`), so `policy_summary()` and the regional
compliance surface cover hosted TTS exactly like hosted ASR. `voice_tts` was
added to the policy map by Agent 4 (coordination: Agent 19 VAULT).

### API-first (hosted) configuration

```dotenv
EV_VOICE_ASR_PROVIDER=openai_compat
EV_VOICE_ASR_BASE_URL=https://api.openai.com/v1   # or any OpenAI-compatible server
EV_VOICE_ASR_API_KEY=<key>
EV_VOICE_ASR_MODEL=whisper-1
EV_ALLOW_REMOTE_ASR=true

EV_VOICE_TTS_PROVIDER=openai_compat
EV_VOICE_TTS_BASE_URL=https://api.openai.com/v1
EV_VOICE_TTS_API_KEY=<key>
EV_VOICE_TTS_MODEL=gpt-4o-mini-tts
EV_ALLOW_REMOTE_TTS=true
```

The round trip (audio in -> transcript -> reply -> `audio_ref` persisted ->
playable `GET /v1/voice/audio/{key}`) is covered end-to-end by
`test_api_first_asr_tts_round_trip_persists_playable_audio`.

Legacy `EV_VOICE_ASR_MODEL` / `EV_VOICE_TTS_MODEL` values apply only to
`openai_compat` / `piper` / `faster_whisper`; the real engine names live in the
new `*_ENGINE` settings, which reconciles the old default mismatches
(`whisper-1` vs `tiny`; `gpt-4o-mini-tts` vs `en_US-lessac-medium.onnx`).

## 4. Offline vs online behavior

| Scenario | ASR | TTS |
| --- | --- | --- |
| No weights, real provider selected | `degraded=True`, confidence 0.0, lifecycle returns `503 asr_degraded` | `degraded=True`, no audio, text reply still returned |
| Audio missing / undecodable | Typed `VoiceError` (422 `asr_audio_required` / `asr_undecodable_audio`) | — |
| Dev doubles (`echo` / `meta`) | Hint echo with confidence 0.0; refuses audio | SSML metadata, no fake duration |
| Remote provider | `EV_ALLOW_REMOTE_ASR=true` required; fails closed otherwise; network/HTTP failure degrades (`degraded=True`, confidence 0.0) — never echoes the hint | `EV_ALLOW_REMOTE_TTS=true` required; fails closed otherwise; network/HTTP failure degrades (no audio, text-only reply) |

A degraded result is never presented as a high-confidence transcript: the
contract pins `degraded=True ⇒ confidence == 0.0`, and the lifecycle refuses to
run chat on a degraded ASR result.

## 5. What leaves the device

| Data | Local default | Remote flag needed |
| --- | --- | --- |
| Utterance audio → ASR | Parakeet ONNX on-device; nothing leaves | `EV_ALLOW_REMOTE_ASR=true` sends bytes to `EV_VOICE_ASR_BASE_URL` |
| Reply text → TTS | Kokoro ONNX on-device; nothing leaves | `EV_ALLOW_REMOTE_TTS=true` sends text to `EV_VOICE_TTS_BASE_URL` |
| Voiceprint samples | Local embeddings only; raw samples discarded after enrollment | `EV_ALLOW_REMOTE_VOICEPRINT_PROCESSING=true` |
| Wake frames | Local keyword/VAD only | never leaves |
| VAD | Local (Silero ONNX when configured, energy double otherwise) | never leaves |

The synthesized audio is persisted in the object store (content-addressed,
`ev://voice/tts/<sha256-prefix>/<sha256>.<ext>`) and served back by the
streaming endpoint; it does not go to any model.

## 6. Streaming transport (SSE)

`POST /v1/voice/utterance/stream` accepts the same body as
`/v1/voice/utterance` and returns `text/event-stream`:

| Event | Payload |
| --- | --- |
| `partial` | `{text, provider, sequence, stable, confidence, degraded, timestamp_ms}` |
| `final_transcript` | `{text, confidence, provider, degraded, audio_ref}` |
| `reply` | full `VoiceUtteranceResponse` (includes `tts.audio_ref`) |
| `error` | `{code, message}` |
| `done` | `{}` |

The shape mirrors the existing SSE convention in `app/api/core.py` (Agent 10).

## 7. Audio persistence & playback

`run_chat_tts_pipeline` persists any synthesized bytes through the object
store and populates `SynthesisResult.audio_ref` / `TtsOut.audio_ref`
(additive API field). Clients then fetch
`GET /v1/voice/audio/{key}` (allowlisted to `voice/**`, Range supported) and
start playback from the first 64 KB chunk, which is the first-playable-latency
boundary for typical TTS clips.

## 8. Barge-in

`POST /v1/voice/sessions/{session_id}/barge_in` stops playback signaling,
cancels the follow-up window, and returns the session to `awake` (listening)
in a single cheap request. Client-side playback must stop on receipt; the
server-side budget target is ≤200 ms for the state transition. A concurrent
utterance pipeline also observes the interrupt and aborts with `barge_in`.

```bash
curl -X POST http://localhost:8000/v1/voice/sessions/<session_id>/barge_in \
  -H "Authorization: Bearer $EV_MASTER_KEY"
```

Clients (Agents 17/18 surfaces) should fire this from their VAD/speech-energy
path while audio is playing, then immediately mute playback and start a fresh
listen.

## 9. Dependency note (Agent 2 — Foundry)

Agent 4 needs the following registry entries + `pyproject` deps. **This
document is the request**; nothing under `backend/app/ml/**` was edited by
Agent 4:

- `asr-parakeet-eou-120m-int8` (on_demand, ~232 MB disk, CC-BY-4.0) + vocab
  artifact handling.
- `asr-parakeet-tdt-v3-int8` (on_demand, opt-in accuracy tier).
- `tts-kokoro-82m-int8` (on_demand, ~82 MB disk, Apache-2.0) + voices pack.
- `tts-chatterbox-nano` (on_demand, ~110 MB, MIT — verify).
- `asr-qwen3-0.6b-mlx` (never-resident subprocess artifact, ~1 GB).
- `kokoro`, `chatterbox`, `mlx`/`mlx-lm` optional deps (Agent 4 imports them
  lazily).
- `faster-whisper` optional dep (Agent 4 measured the 2026-08-12 clean-subset
  WER with `Systran/faster-whisper-base.en`; it is the documented local
  fallback until the Parakeet artifact is registered).
- `voice_tts` has been added to `compliance.policy` remote tracks (Agent 19
  owns the file; Agent 4 made the additive change on this order and requests a
  review for the consent-track naming).
- `ev-eval asr` is wired (Agent 2) and drives `eval.ml.asr_eval:main`; it can
  also run directly via
  `uv run python -m eval.ml.asr_eval --data-root <root> --samples N`.

## 10. Measured performance

| Metric | Target | Measured (this host, 2026-08-12) |
| --- | --- | --- |
| WER — LibriSpeech test-clean | ≤8% | **5.90%** (`faster-whisper` base.en, 50 clips, 23 speakers, 0 degraded; `eval/ml/asr_quality.json` `measured:true`, 2026-08-12). Hosted endpoint still unmeasured: OpenAI key 401, OpenRouter key 403 key-limit — no valid audio credential on this host. Parakeet vendor claims ~2.8% (unverified) |
| WER — 30 min owner speech | ≤12% | **not measured** — needs enrolled corpus |
| First ASR partial | ≤300 ms | **not measured** — `openai_compat` `/audio/transcriptions` is a final-only Whisper-style call (no provider partials); first partial needs a streaming-capable hosted endpoint or local Parakeet weights, plus a valid credential |
| Final transcript latency | ≤1.2× audio | local faster-whisper measured: mean 3885 ms / median 3223 ms, mean ratio **0.50× audio** (target met) over 50 clips; first clip includes model warm-up |
| TTS first audio chunk | ≤500 ms | **not measured for synthesis** — hosted probes 403 (OpenRouter key limit), 401 (OpenAI); local object-store first chunk measured 0.04 ms |
| Barge-in cancel | ≤200 ms | API transition verified in tests; wall-clock not measured on device |
| Offline suite, zero weights | green (skips, not failures) | verified for voice ASR/TTS units; full-suite blocked by other agents' in-flight modules (see report) |

### Local vs hosted TTS — written recommendation

**Recommendation: keep local Kokoro-82M INT8 as the default** (once Agent 2
registers the ~82 MB artifact). Reasons:

1. **Privacy/offline**: reply text never leaves the device; works with no
   network and no per-reply cost.
2. **Latency**: hosted probes on this host measured 625–1516 ms for failed
   round trips alone and are currently 403 (OpenRouter key limit) / 401
   (OpenAI); local object-store playback measured 0.04 ms first chunk. Kokoro
   synthesis itself is unmeasured until weights land, but it removes the
   network leg entirely.
3. **Cost**: per-reply cost could not be measured (no valid hosted key, so
   there is no number that beats local). Local Kokoro costs zero per reply;
   hosted TTS bills per character (verify rates with the provider when
   credentials land).

If a valid hosted key is available and the owner's reply volume is low, hosted
TTS is a fine opt-in (gate: `EV_ALLOW_REMOTE_TTS=true`); it is not the default
recommendation for a companion that speaks constantly.

### Measurement procedure

```bash
cd backend
uv sync --extra ml --extra mlx --extra dev
uv run python -m app.ml.cli pull asr-parakeet-eou-120m-int8
uv run python -m app.ml.cli pull tts-kokoro-82m-int8

# 0. WER harness (already present): download + extract OpenSLR test-clean
#    (CC BY 4.0) under a data root, then run against the configured provider:
#    curl -L -o test-clean.tar.gz https://www.openslr.org/resources/12/test-clean.tar.gz
#    tar xzf test-clean.tar.gz -C /path/to/root
#    uv run python -m eval.ml.asr_eval --data-root /path/to/root --samples 50
#    (aliased as `ev-eval asr`; faster-whisper is a valid local engine when
#    the Parakeet artifact is not yet registered)
#    Target: <= 8% WER on the subset.
# 2. WER on 30 min of the owner's consented speech. Target: <= 12%.
# 3. Streaming partials: measure time from first audio frame to the first
#    `partial` SSE event. Target: <= 300 ms.
# 4. Final latency: wall-clock to the `final_transcript` event divided by
#    audio duration. Target: <= 1.2x.
# 5. TTS: time from POST /v1/voice/utterance to the first byte of
#    GET /v1/voice/audio/{key}. Target: <= 500 ms.
#    Direct hosted first-byte probe (needs a valid key + EV_ALLOW_REMOTE_TTS=true):
#    curl -o /dev/null -sS -w 'first_byte=%{time_starttransfer}s\n' \
#      -X POST "$EV_VOICE_TTS_BASE_URL/audio/speech" \
#      -H "Authorization: Bearer $EV_VOICE_TTS_API_KEY" \
#      -H 'Content-Type: application/json' \
#      -d '{"model":"gpt-4o-mini-tts","voice":"alloy","input":"Hello","response_format":"mp3"}'
#    One-command probe (writes eval/ml/voice_latency.json; records
#    measured:false with the auth reason when the key is invalid):
#    cd backend && uv run python -m eval.ml.voice_latency_probe --audio <flac>
# 6. First ASR partial over a real network: requires a streaming-capable
#    OpenAI-compatible endpoint; the current non-streaming Whisper-style
#    transcription returns only the final transcript.
# 7. Barge-in: time from POST /v1/voice/sessions/{id}/barge_in to the 200 OK
#    with state=awake. Target: <= 200 ms.
```

Record the numbers in this table and in the report footer. No fabricated
WER/latency figures are ever reported; the current artifact records
`measured: true` (faster-whisper base.en, 5.90% WER, 2026-08-12). Hosted
first-partial and TTS first-chunk latencies remain unmeasured until a valid
hosted audio credential exists (OpenAI key 401; OpenRouter key limit 403).

## 11. Tests

```bash
cd backend
uv run pytest tests/test_voice_asr.py tests/test_voice_tts.py \
  tests/test_voice_lifecycle.py tests/test_voice_providers.py -q
uv run pytest -q
uv run ruff check app clients tests
uv run mypy app clients
```

- `test_voice_asr.py` — Parakeet factory entry point, degraded semantics
  (including hosted network failure -> degraded, never hint-echo), streaming
  partials, fail-closed echo/remote/traversal, VAD wiring.
- `test_voice_tts.py` — Kokoro factory, degraded semantics, WAV synthesis with
  a fake pipeline, remote TTS gate (factory + class), hosted network failure
  degradation, no fabricated durations.
- `test_voice_lifecycle.py` — full lifecycle, barge-in, degraded-ASR 503,
  SSE streaming, API-first ASR+TTS round trip with persisted playable audio,
  follow-up timer reset, no re-wake inside ACTIVE/FOLLOW_UP, sleep phrases,
  non-owner/silence addressivity rejection, push-to-talk bypass, and the
  configurable follow-up timeout.
- `test_voice_live.py` — EV LIVE turn-taking (thinking vs complete vs
  trailing pauses), barge-in cancel, backchannels, behavior envelopes,
  deep-work routing, sleep-phrase close, and `WS /v1/voice/live` registration.

## 12. Still needs a human

- Weight pulls + ONNX graph validation for the Parakeet EOU export (the
  adapter implements the standard NeMo contract; first run with real weights
  must confirm input/output names and the vocab map).
- A valid hosted ASR/TTS credential (or Agent 2's Parakeet/Kokoro registry
  entries + weights) to measure hosted first-partial and TTS first-chunk
  latencies and to replace faster-whisper as the intended local engine. The
  clean-subset WER is already measured (5.90%).
- A consented 30-minute owner-speech corpus for the ≤12% WER gate.
- Microphone/playback wiring on target devices (Agent 3/17/18 surface work).
