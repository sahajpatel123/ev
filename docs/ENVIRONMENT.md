# EV — Environment Configuration Reference

Every `EV_*` key the system reads, with defaults and meanings. `.env.example`
at the repository root is the quickstart subset and currently declares **69
entries** (67 unique keys; `EV_LOCAL_MODEL_BASE_URL` and `EV_LOCAL_MODEL_NAME`
appear twice, once under the local-model section and once under the model
logging section). The sections below cover those 69 plus every additional key
the server, CLI, and collectors read.

## How configuration loads

- **Server settings** are declared in `backend/app/config.py`
  (pydantic-settings, `env_prefix="EV_"`, `.env` file, `extra="ignore"`).
  Keys are case-insensitive `EV_` + the SCREAMING_SNAKE form of the field.
- **Compliance policy** (`backend/app/compliance/policy.py`), the **sandbox
  root** (`backend/app/tools/sandbox.py`), and the **runtime daemon sweep
  cadence** (`backend/app/workers/runtime_daemon.py`) read `EV_*` directly
  with `os.getenv`.
- **Client-side keys** (`EV_API_URL`, `EV_API_KEY`, collector/listener keys)
  are read by `backend/clients/*` and documented in the client section below.
- The **E2E harness** (`backend/app/scripts/e2e_cli.py`) accepts `EV_E2E_*`
  overrides; those are test-only and listed at the end.

## 1. Core server

| Key | Default | Values | Purpose |
| --- | --- | --- | --- |
| `EV_APP_NAME` | `EV` | string | FastAPI title and root status name |
| `EV_ENVIRONMENT` | `dev` | `dev` / `test` / `prod` | Deployment environment label |
| `EV_PERSONA_NAME` | `EV` | string | Identity compiled into the prompt; swapping models never changes who EV is |
| `EV_PERSONA_DESCRIPTION` | `the user's persistent personal AI companion` | string | Persona description compiled into the identity block |
| `EV_MASTER_KEY` | `ev-local-dev-key` (code) / `change-me` (example) | string | Single-user master key; required on every API call and for master-only actions. Change it before deploying. |
| `EV_CORS_ORIGINS` | `["*"]` | JSON list | Allowed CORS origins |
| `EV_ACCESS_LOG_ENABLED` | `true` | boolean | Master switch for the append-only access log |

## 2. Database, queue & processing

| Key | Default | Values | Purpose |
| --- | --- | --- | --- |
| `EV_DATABASE_URL` | `sqlite+aiosqlite:///./ev.db` | SQLAlchemy URL | Primary store; compose uses `postgresql+psycopg://ev:ev@db:5432/ev` |
| `EV_REDIS_URL` | `redis://localhost:6379/0` | Redis URL | RQ queues and runtime/cache state |
| `EV_PROCESSING_MODE` | `sync` | `sync` / `queue` | `sync` processes events inline (no Redis needed); `queue` enqueues RQ jobs consumed by the `worker` service |

## 3. Embeddings

| Key | Default | Values | Purpose |
| --- | --- | --- | --- |
| `EV_EMBEDDING_PROVIDER` | `hash` | `hash` / `http` | `hash` = offline deterministic embeddings; `http` = OpenAI-compatible embedding API |
| `EV_EMBEDDING_BASE_URL` | — | URL | Embedding API base URL (required for `http`) |
| `EV_EMBEDDING_API_KEY` | — | string | Embedding API key |
| `EV_EMBEDDING_MODEL` | `text-embedding-3-small` | string | Embedding model name |
| `EV_EMBEDDING_DIM` | `384` | int | Vector dimension; tests/eval use `64` |

## 4. Web research

| Key | Default | Values | Purpose |
| --- | --- | --- | --- |
| `EV_SEARCH_PROVIDER` | `none` | `none` / `mock` / `brave` | `none` = memory-only (no key, no network); `mock` = tests; `brave` = Brave Search API |
| `EV_BRAVE_SEARCH_BASE_URL` | `https://api.search.brave.com/res/v1/web/search` | URL | Brave Search endpoint |
| `EV_BRAVE_SEARCH_API_KEY` | — | string | User-supplied Brave key; required for `brave` |
| `EV_SEARCH_TIMEOUT_SECONDS` | `10.0` | float | Per-search HTTP timeout |
| `EV_SEARCH_RESULT_LIMIT` | `5` | int | Max normalized results per search |

## 5. Voiceprint enrollment, verification & wake word

Biometric data: retention and remote processing are additionally governed by
the compliance keys in §13.

| Key | Default | Values | Purpose |
| --- | --- | --- | --- |
| `EV_VOICEPRINT_PROVIDER` | `hash` | `hash` / `speechbrain` / `http` | `hash` = offline dev/test embeddings; `speechbrain` = local SpeechBrain ECAPA-TDNN (`spkrec-ecapa-voxceleb`); `http` = remote encoder service |
| `EV_VOICEPRINT_BASE_URL` | — | URL | Encoder service URL (required for `http`) |
| `EV_VOICEPRINT_API_KEY` | — | string | Encoder API key |
| `EV_VOICEPRINT_MODEL` | `speechbrain/spkrec-ecapa-voxceleb` | string | SpeechBrain source / encoder model name |
| `EV_VOICEPRINT_MODEL_DIR` | — | path | SpeechBrain model cache/download directory |
| `EV_VOICEPRINT_DIM` | `192` | int | Embedding dimension |
| `EV_VOICEPRINT_THRESHOLD` | `0.72` | float | Similarity threshold for owner verification |
| `EV_VOICE_WAKE_PROVIDER` | `phrase` | `phrase` / `porcupine` / `silero_vad` / `openwakeword` | `phrase` = deterministic text-hint/frame matcher (dev/test); `porcupine` = Picovoice Porcupine; `silero_vad` = Porcupine/phrase keyword engine gated by Silero VAD; `openwakeword` = custom "EVIE" ONNX head |
| `EV_VOICE_WAKE_ACCESS_KEY` | — | string | Picovoice access key (required for `porcupine`/`silero_vad` with Porcupine) |
| `EV_VOICE_WAKE_MODEL_PATH` | — | path | Custom "EVIE" Porcupine `.ppn` model |
| `EV_VOICE_WAKE_PORCUPINE_LIBRARY_PATH` | — | path | Optional custom Porcupine native library |
| `EV_VOICE_WAKE_SENSITIVITY` | `0.6` | float | Porcupine sensitivity (higher = more trigger-prone) |
| `EV_VOICE_WAKE_VAD_MODEL_PATH` | — | path | Silero VAD JIT model path |
| `EV_VOICE_WAKE_VAD_THRESHOLD` | `0.5` | float | Minimum mean VAD speech probability to accept a wake |
| `EV_VOICE_WAKE_OPENWAKEWORD_MODEL_PATH` | — | path | Custom "EVIE" openWakeWord `.onnx` head (train via `make wake-train`); when missing the ears process falls back to the local Whisper spotter |
| `EV_VOICE_WAKE_OPENWAKEWORD_VERIFIER_PATH` | — | path | Logistic-regression verifier weights for the head |
| `EV_VOICE_WAKE_OPENWAKEWORD_THRESHOLD` | `0.5` | float | openWakeWord activation threshold |
| `EV_EARS_WAKE_LOCAL_SPOTTER` | `true` | boolean | On-device wake fallback: run a small local faster-whisper model in the ears process to spot "EVIE" when the openWakeWord head is absent, and have the server trust its confidence (no server-side Whisper pass) |
| `EV_EARS_WAKE_ASR_MODEL` | `tiny` | string | Dedicated fast faster-whisper model for on-device wake spotting |
| `EV_EARS_STUCK_LOOP_DROP` | `true` | boolean | Drop VAD segments whose content literally repeats (stuck mic / self-echo) before they reach ASR |
| `EV_EARS_STUCK_LOOP_THRESHOLD` | `0.10` | float | Normalized self-difference below which a segment counts as a loop |
| `EV_EARS_STREAM_PLAYBACK` | `true` | boolean | Stream TTS chunks over SSE and play each sentence as it arrives, instead of waiting for the full reply |

## 6. Voice ASR & TTS

| Key | Default | Values | Purpose |
| --- | --- | --- | --- |
| `EV_VOICE_ASR_PROVIDER` | `echo` | `echo` / `faster_whisper` / `openai_compat` | `echo` = offline transcript hints; `faster_whisper` = local Whisper-class transcription; `openai_compat` = any OpenAI-compatible `/audio/transcriptions` endpoint (requires `EV_ALLOW_REMOTE_ASR=true`) |
| `EV_VOICE_ASR_BASE_URL` | — | URL | ASR endpoint (required for `openai_compat`) |
| `EV_VOICE_ASR_API_KEY` | — | string | ASR API key |
| `EV_VOICE_ASR_MODEL` | `whisper-1` | string | ASR model name (`whisper-1` for remote; e.g. `tiny`/`small` for faster-whisper) |
| `EV_VOICE_ASR_MODEL_DIR` | — | path | faster-whisper model cache/download directory |
| `EV_VOICE_ASR_DEVICE` | `auto` | `auto` / `cpu` / `cuda` | faster-whisper compute device |
| `EV_VOICE_ASR_COMPUTE_TYPE` | `auto` | `auto` / `int8` / `float16` / `float32` | faster-whisper compute type |
| `EV_VOICE_ASR_LANGUAGE` | — | language code | Default transcription language; per-request `language` overrides |
| `EV_VOICE_ASR_VAD_FILTER` | `true` | boolean | faster-whisper VAD filter on the input audio |
| `EV_VOICE_TTS_PROVIDER` | `meta` | `meta` / `piper` / `openai_compat` | `meta` = offline SSML metadata; `piper` = local Piper ONNX voice; `openai_compat` = any OpenAI-compatible `/audio/speech` endpoint |
| `EV_VOICE_TTS_BASE_URL` | — | URL | TTS endpoint (required for `openai_compat`) |
| `EV_VOICE_TTS_API_KEY` | — | string | TTS API key |
| `EV_VOICE_TTS_MODEL` | `gpt-4o-mini-tts` | string | TTS model name (`gpt-4o-mini-tts` for remote; `.onnx` voice path for `piper`) |
| `EV_VOICE_TTS_MODEL_DIR` | — | path | Piper voice model directory |
| `EV_VOICE_TTS_BINARY` | `piper` | executable | Piper CLI binary name/path |
| `EV_VOICE_TTS_VOICE` | `alloy` | string | Voice id; urgency/warmth/brevity map to speed + spoken instructions while the voice stays consistent |
| `EV_VOICE_TTS_FORMAT` | `mp3` | audio format | Synthesized audio format |
| `EV_VOICE_TTS_LENGTH_SCALE` | `1.0` | float | Piper base length scale (style-adjusted) |
| `EV_VOICE_TTS_NOISE_SCALE` | `0.667` | float | Piper base noise scale (style-adjusted) |
| `EV_VOICE_TTS_SENTENCE_SILENCE` | `0.2` | float | Piper base sentence silence (style-adjusted) |

## 7. Chat gateway & local models

| Key | Default | Values | Purpose |
| --- | --- | --- | --- |
| `EV_CHAT_PROVIDER` | `echo` | `echo` / `mock` / `deepseek` / `xai` / `local` / `opencode` | Typed chat / HUD / HTTP utterance. Daily: `deepseek`. Live speech is `EV_VOICE_LIVE_BRAIN`, not this. |
| `EV_DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | URL | Official DeepSeek chat endpoint |
| `EV_DEEPSEEK_API_KEY` | — | string | Official DeepSeek API key (`platform.deepseek.com`) |
| `EV_DEEPSEEK_MODEL` | `deepseek-v4-flash` | string | Official API model id (not the Hugging Face checkpoint name) |
| `EV_DEEPSEEK_THINKING` | `false` | boolean | V4 thinking/CoT. Off for spoken-speed voice replies |
| `EV_XAI_BASE_URL` | `https://api.x.ai/v1` | URL | Official xAI OpenAI-compatible chat endpoint |
| `EV_XAI_API_KEY` | — | string | Official xAI API key (`console.x.ai`) |
| `EV_XAI_MODEL` | `grok-4.6` | string | Typed chat / tools / HUD model. Not the voice model. |
| `EV_XAI_VOICE_MODEL` | `grok-voice-think-fast-2.0` | string | Live speech-to-speech model on `wss://api.x.ai/v1/realtime` |
| `EV_XAI_VOICE_VOICE` | `eve` | string | Built-in Grok Voice roster id |
| `EV_XAI_VOICE_VAD_THRESHOLD` | `0.72` | float | Live server VAD. Higher resists speaker echo cutting her off |
| `EV_XAI_VOICE_SILENCE_MS` | `550` | int | Pause allowed before Grok Voice ends your turn |
| `EV_VOICE_LIVE_BRAIN` | `auto` | `auto` / `openai` / `xai` / `pipeline` | `auto` uses OpenAI Realtime when `EV_OPENAI_API_KEY` is set, else Grok Voice when `EV_XAI_API_KEY` is set |
| `EV_OPENAI_API_KEY` | — | string | Official OpenAI API key (`platform.openai.com`). Live talk, not typed chat. |
| `EV_OPENAI_REALTIME_MODEL` | `gpt-realtime-2.1-mini` | string | OpenAI Realtime speech-to-speech model. Live function tools come from the current runtime capability projection and are rechecked by policy before dispatch. |
| `EV_OPENAI_REALTIME_URL` | `wss://api.openai.com/v1/realtime` | URL | OpenAI Realtime WebSocket |
| `EV_LOCAL_MODEL_BASE_URL` | `http://localhost:11434/v1` | URL | OpenAI-compatible local server (Ollama/llama.cpp) used when `EV_CHAT_PROVIDER=local` |
| `EV_LOCAL_MODEL_NAME` | `llama3` | string | Local model name |
| `EV_MODEL_CALL_LOG_ENABLED` | `true` | boolean | Persist every gateway call to `model_calls` for audit |

## 7b. Vision, camera look, OCR

| Key | Default | Values | Purpose |
| --- | --- | --- | --- |
| `EV_VISION_PROVIDER` | `deterministic` | `deterministic` / `tesseract` / `apple_vision` / `deepseek_ocr` | OCR engine. Darwin auto-selects Apple Vision when `evvision` exists (not under pytest). |
| `EV_VISION_TESSERACT_BINARY` | `tesseract` | path | Tesseract binary when `EV_VISION_PROVIDER=tesseract` |
| `EV_VISION_DEEPSEEK_OCR_URL` | — | URL | Self-hosted DeepSeek-OCR HTTP endpoint. Official `api.deepseek.com` is refused (text-only). |
| `EV_VISION_DEEPSEEK_OCR_TIMEOUT` | `20` | seconds | Hosted OCR timeout |

The live `look` tool takes **one** consented camera frame (or an owner photo), runs OCR + object labels on-device, names only enrolled/consented matches, and may use DeepSeek **chat** to polish the spoken sentence from derived text. Raw pixels are not sent to `api.deepseek.com`.

## 8. Intelligence filter

| Key | Default | Values | Purpose |
| --- | --- | --- | --- |
| `EV_FILTER_CRITIC_ENABLED` | `false` | boolean | Enable the provider-backed critic refinement loop |
| `EV_FILTER_CRITIC_MAX_ITERATIONS` | `2` | int | Maximum critic refinement iterations |
| `EV_FILTER_CRITIC_MODES` | `coaching,emergency,analytical` | comma-separated modes | Which interaction modes use the critic (staged trust) |

## 9. Context & retrieval

| Key | Default | Values | Purpose |
| --- | --- | --- | --- |
| `EV_CONTEXT_BUDGET_TOKENS` | `20000` | int | Cap on assembled prompt context |
| `EV_MAX_RETRIEVAL_MEMORIES` | `50` | int | Candidate cap for hybrid retrieval |
| `EV_ROLLUP_BUDGET_TOKENS` | `1500` | int | Budget for the rolling summary section |
| `EV_STANDARD_HISTORY_TURNS` | `10` | int | Continuous history window (default depth) |
| `EV_DEEP_HISTORY_TURNS` | `40` | int | History window for the deep depth profile |
| `EV_DEEPEST_HISTORY_TURNS` | `150` | int | History window for the deepest depth profile |
| `EV_DEEP_RETRIEVAL_MEMORIES` | `100` | int | Retrieval candidates at deep depth |
| `EV_DEEPEST_RETRIEVAL_MEMORIES` | `150` | int | Retrieval candidates at deepest depth |

## 10. Quiet hours & attention budget

| Key | Default | Values | Purpose |
| --- | --- | --- | --- |
| `EV_QUIET_HOURS_START` | `22:00` | `HH:MM` | Quiet hours start (local time) |
| `EV_QUIET_HOURS_END` | `08:00` | `HH:MM` | Quiet hours end |
| `EV_DAILY_ALERT_BUDGET` | `5` | int | Base daily proactive-alert budget (calibration may adjust) |

## 11. 24/7 runtime state machine

| Key | Default | Values | Purpose |
| --- | --- | --- | --- |
| `EV_RUNTIME_VERIFY_TIMEOUT_SECONDS` | `15` | int | Max time in the verifying state |
| `EV_RUNTIME_AWAKE_TIMEOUT_SECONDS` | `120` | int | REST hint for time spent awake before first turn |
| `EV_RUNTIME_PROCESSING_TIMEOUT_SECONDS` | `90` | int | Max processing time |
| `EV_RUNTIME_RESPOND_TIMEOUT_SECONDS` | `60` | int | Max responding time |
| `EV_RUNTIME_FOLLOWUP_TIMEOUT_SECONDS` | `30` | int | REST follow-up hint; does not close the session |
| `EV_RUNTIME_SESSION_TIMEOUT_SECONDS` | `900` | int | Long-idle lock for listening (`awake` / `follow_up`) |
| `EV_RUNTIME_HEARTBEAT_GRACE_SECONDS` | `300` | int | Device heartbeat grace before considered offline |
| `EV_RUNTIME_DLQ_MAX_ATTEMPTS` | `3` | int | Max retries before a dead letter is terminal |
| `EV_RUNTIME_URGENT_PRIORITY_THRESHOLD` | `0.7` | float | Priority needed to interrupt during quiet hours |
| `EV_RUNTIME_FOCUS_URGENT_THRESHOLD` | `0.85` | float | Priority needed to interrupt during active focus |
| `EV_RUNTIME_DAEMON_TICK_SECONDS` | `30` | int | `runtime` daemon tick interval |
| `EV_RUNTIME_HEALTHCHECK_MAX_AGE_SECONDS` | `120` | int | `runtime` compose healthcheck: max age of the latest `daemon` RuntimeEvent |

## 12. Scheduler & routines

| Key | Default | Values | Purpose |
| --- | --- | --- | --- |
| `EV_SCHEDULER_TICK_SECONDS` | `60` | int | `scheduler` tick interval for routines/automations |
| `EV_SCHEDULER_MAX_DAYS_LOOKAHEAD` | `366` | int | Declared scheduler lookahead window (reserved) |
| `EV_AUTOMATION_FAILURE_THRESHOLD` | `3` | int | Consecutive routine failures before an alert |

## 13. Compliance, retention & residency

Region drives default retention windows and disclosure text. Remote-processing
gates default to **denied** (fail closed); enable only when the corresponding
consent has been obtained.

| Key | Default | Values | Purpose |
| --- | --- | --- | --- |
| `EV_REGION` | `global` | `global` / `eu` / `uk` / `us` / `us-il` / `in` | Regional policy for retention windows and disclosures |
| `EV_RETENTION_VOICEPRINT_DAYS` | `-1` | days (`-1` keep, `0` destroy) | Voiceprint retention window |
| `EV_RETENTION_TRAINING_SNAPSHOT_DAYS` | `-1` | days | Training corpus snapshot retention |
| `EV_RETENTION_LIVE_AUDIO_DAYS` | `0` | days | Live audio event retention |
| `EV_RETENTION_ACCESS_LOG_DAYS` | `730` | days | Access-log retention |
| `EV_RETENTION_EVENT_DAYS` | `-1` | days | Raw event retention |
| `EV_RETENTION_INTEGRATION_CACHE_DAYS` | `0` | days | Integration cache retention |
| `EV_RESIDENCY_MODE` | `local` | `local` / `region` / `cloud` | Where data may be processed/reside |
| `EV_ALLOW_REMOTE_VOICEPRINT_PROCESSING` | `false` | boolean | Remote voiceprint encoder processing gate |
| `EV_ALLOW_REMOTE_ASR` | `false` | boolean | Remote speech-to-text (ASR) processing gate |
| `EV_ALLOW_REMOTE_TRAINING` | `false` | boolean | Remote training/adapter processing gate |
| `EV_ALLOW_REMOTE_LIFE_DATA` | `false` | boolean | Remote life-data processing gate |
| `EV_ALLOW_REMOTE_FILTER_TRAINING` | `false` | boolean | Remote filter self-improvement gate |
| `EV_COMPLIANCE_SWEEP_HOURS` | `24` | hours (`<= 0` disables) | Cadence of the daemon's scheduled retention sweep |

## 14. Object storage & maintenance

| Key | Default | Values | Purpose |
| --- | --- | --- | --- |
| `EV_OBJECT_STORE_BACKEND` | `local` | `local` / `s3` | Blob backend |
| `EV_STORAGE_ROOT` | `./storage` | path | Local blob root |
| `EV_S3_ENDPOINT_URL` | — | URL | S3-compatible endpoint (compose: `http://minio:9000`) |
| `EV_S3_BUCKET` | `ev` | string | Bucket name |
| `EV_S3_ACCESS_KEY` | — | string | S3 access key |
| `EV_S3_SECRET_KEY` | — | string | S3 secret key |
| `EV_TOMBSTONE_BLOB_RETENTION_DAYS` | `30` | days | How long tombstoned blobs are kept before purge |
| `EV_BACKUP_RETENTION_COUNT` | `7` | int | Backup snapshots kept |
| `EV_BACKUP_PASSPHRASE` | — | string | User-held backup passphrase; deliberately separate from `EV_MASTER_KEY` |
| `EV_SANDBOX_ROOT` | `{storage_root}/sandbox` | path | Root for `/v1/tools/execute` and file tools (traversal is rejected outside it). This is a process-level jail, not a security boundary: it runs with the server user's privileges, so it is only safe for owner-trusted local callers — containerize before exposing to untrusted/external code (see `docs/SECURITY.md` §13) |

## 15. Integrations, webhooks & plugins

| Key | Default | Values | Purpose |
| --- | --- | --- | --- |
| `EV_VAULT_KEY` | required | string | Encrypts integration OAuth tokens/webhook secrets. Required (min 16 chars); never derived from `EV_MASTER_KEY` — a leaked master key must not decrypt the vault |
| `EV_WEBHOOK_MAX_SKEW_SECONDS` | `300` | int | Max timestamp skew for webhook signatures (replay protection) |
| `EV_WEBHOOK_RATE_LIMIT` | `60` | int | Webhook requests allowed per window |
| `EV_WEBHOOK_WINDOW_SECONDS` | `60` | int | Rate-limit window |
| `EV_WEBHOOK_MAX_BODY_BYTES` | `1048576` | int | Max webhook payload size |
| `EV_PLUGIN_TIMEOUT_SECONDS` | `3` | int | Plugin subprocess hard timeout |
| `EV_PLUGIN_MAX_OUTPUT_BYTES` | `65536` | int | Plugin output cap |

## 16. Client environment (CLI, listener, collectors)

These keys are read by `backend/clients/*` and the iOS Swift package uses the
same contract through app settings.

| Key | Default | Values | Purpose |
| --- | --- | --- | --- |
| `EV_API_URL` | `http://127.0.0.1:8000` | URL | Backend base URL for CLI, listener, and collector agent |
| `EV_API_KEY` | — | string (required) | Master key or registered-device token for authenticated calls |
| `EV_CLI_QUEUE_DIR` | `~/.ev/queue` | path | Offline capture queue location (`ev queue` / `ev sync`) |
| `EV_DEVICE_ID` | — | string | Device id sent with listener/collector events |
| `EV_LISTENER_INTERVAL` | `30` | seconds | Device listener polling interval |
| `EV_LISTENER_QUEUE_DIR` | `~/.ev/listener_queue` | path | Offline capture queue location for the device listener |
| `EV_LISTENER_LIVE_CHANNEL` | `listener` | string | Default live channel for `--live-capture` |
| `EV_LISTENER_LIVE_KIND` | `app` | `app` / `screen` / `audio` / `health` / `vision` / `location` | Default live channel kind for `--live-capture` |
| `EV_LISTENER_LIVE_EVENT_TYPE` | `note` | string | Default live event type for `--live-capture` |
| `EV_CHALLENGE_PHRASE` | — | string | One-shot `--challenge-phrase` default for listener voice verification |
| `EV_VERIFY_SAMPLE` | — | path/string | One-shot `--verify-sample` default |
| `EV_SAY` | — | string | One-shot `--say` default (what EV should say) |
| `EV_FOLLOW_UP_SAY` | — | string | One-shot `--follow-up-say` default |
| `EV_AUDIO_SCENE` | — | string | Derived scene hint (`speech`, `music`, `noise`, …); overrides hint file |
| `EV_AUDIO_CONFIDENCE` | — | float 0–1 | Scene confidence; overrides hint file |
| `EV_IN_CALL` | — | `1`/`true`/`yes` | In-call flag; overrides hint file |
| `EV_AUDIO_SCENE_FILE` | `~/.ev/audio-scene.json` | path | User-managed `{scene, in_call, confidence}` hint file |
| `EV_LOCATION_PLACE` | — | string | Coarse place label; overrides location file |
| `EV_LOCATION_PRESENCE` | — | string | Presence label; overrides location file |
| `EV_LOCATION_FILE` | `~/.ev/location.json` | path | User-managed `{place, presence}` file (never exact coordinates) |
| `EV_LIVE_PRIVACY` | `normal` | `normal` / `sensitive` / `private` / `never_send_to_model` | Privacy level the collector posts live events with |

## 17. E2E test harness (test-only)

`backend/app/scripts/e2e_cli.py` accepts `EV_E2E_BASE_URL`, `EV_E2E_MASTER_KEY`,
`EV_E2E_DATABASE_URL`, `EV_E2E_PROCESSING_MODE`, `EV_E2E_CHAT_PROVIDER`,
`EV_E2E_EMBEDDING_PROVIDER`, `EV_E2E_EMBEDDING_DIM`, `EV_E2E_STORAGE_ROOT`,
`EV_E2E_VAULT_KEY`, `EV_E2E_EXPECT_QUEUE`, `EV_E2E_QUEUE_TIMEOUT`,
`EV_E2E_SCHEDULER_TIMEOUT`, and `EV_E2E_DAEMON_TIMEOUT`. These override the
server configuration only when spawning an embedded server for end-to-end CLI
validation; they are not runtime settings.

## Changing settings

The server reads `.env` from the working directory or the process environment.
After changing `.env`, restart the affected service (`docker compose restart
api worker scheduler runtime`, or restart `uvicorn` in dev).

## Agent 4 — production voice engines (additive)

The real on-device engines are Parakeet-EOU-120M INT8 ONNX (ASR) and
Kokoro-82M INT8 ONNX (TTS). Their canonical identifiers are independent of the
legacy `EV_VOICE_ASR_MODEL` / `EV_VOICE_TTS_MODEL` values, which remain for
`openai_compat` / `piper` only.

| Key | Default | Values | Purpose |
| --- | --- | --- | --- |
| `EV_VOICE_ASR_PROVIDER` | `echo` | `echo` / `parakeet` / `parakeet_tdt` / `faster_whisper` / `openai_compat` | `parakeet` = Parakeet-EOU-120M INT8 ONNX (default real engine); `parakeet_tdt` = opt-in TDT-v3 accuracy tier; `echo` remains the offline dev/test double |
| `EV_VOICE_ASR_ENGINE` | `parakeet-eou-120m-int8` | string | Canonical Parakeet engine/registry name |
| `EV_VOICE_ASR_ALT_ENGINE` | `parakeet-tdt-v3-int8` | string | Opt-in Parakeet-TDT-v3 registry name |
| `EV_VOICE_ASR_ONNX_PATH` | — | path | Explicit Parakeet `.onnx` path (otherwise resolved under `EV_ML_MODEL_DIR` / `EV_VOICE_ASR_MODEL_DIR`) |
| `EV_VOICE_ASR_STREAM_CHUNK_MS` | `200` | ms | Streaming partial cadence (keeps first partial under the 300 ms target) |
| `EV_VOICE_ASR_ALLOWED_ROOTS` | — | JSON list of paths | Allowlisted directories for client-supplied `audio_ref` paths; system temp and `EV_STORAGE_ROOT/voice` are always allowed |
| `EV_VOICE_TTS_PROVIDER` | `meta` | `meta` / `kokoro` / `chatterbox` / `piper` / `openai_compat` | `kokoro` = Kokoro-82M INT8 ONNX (default real engine); `chatterbox` = opt-in Chatterbox-Nano expressive tier |
| `EV_VOICE_TTS_ENGINE` | `kokoro-82m-int8` | string | Canonical Kokoro engine/registry name |
| `EV_VOICE_TTS_KOKORO_VOICE` | `af_heart` | string | Kokoro voice id (one of the 54 bundled voices) |
| `EV_VOICE_CHATTERBOX_ENGINE` | `chatterbox-nano` | string | Chatterbox-Nano registry name |
| `EV_VOICE_CHATTERBOX_VOICE` | `default` | string | Chatterbox speaker id / cloned-voice reference |
| `EV_ALLOW_REMOTE_TTS` | `false` | boolean | Remote `/audio/speech` gate; fail-closed like `EV_ALLOW_REMOTE_ASR` |

## Agent 14 — notification delivery (PULSE, additive)

| Key | Default | Values | Purpose |
| --- | --- | --- | --- |
| `EV_NOTIFY_BACKEND` | `console` | `console` / `macos` / `webhook` / `apns` | Delivery backend; console is the CI/dev double |
| `EV_NOTIFY_MACOS_HELPER_PATH` | — | path | Prebuilt `EVNotificationHelper` binary (skips build) |
| `EV_NOTIFY_MACOS_BUILD_DIR` | `./storage/notify` | path | Where the Swift helper is compiled once |
| `EV_NOTIFY_MACOS_BUNDLE_ID` | `ev.pulse` | string | Bundle identity used for Notification Center permission |
| `EV_NOTIFY_MACOS_ALLOW_OSASCRIPT` | `true` | boolean | Last-resort `display notification` fallback when UNUserNotificationCenter is unavailable (sandboxed helper, unsigned context) |
| `EV_NOTIFY_WEBHOOK_URL` | — | URL | Signed webhook endpoint for the webhook backend |
| `EV_NOTIFY_WEBHOOK_SECRET` | — | string | HMAC-SHA256 secret; header `X-EV-Signature: sha256=…` |
| `EV_NOTIFY_APNS_ENABLED` | `false` | boolean | APNs path is wired; enable after SUIT uploads a device token |
| `EV_NOTIFY_APNS_KEY_ID` / `TEAM_ID` / `TOPIC` / `KEY_PATH` | — | APNs | ES256 signing identity for real APNs delivery |
| `EV_NOTIFY_MAX_ATTEMPTS` | `3` | int | Failed-delivery limit before `max_attempts` suppression |
| `EV_NOTIFY_RETRY_INTERVAL_SECONDS` | `300` | seconds | Backoff between alert delivery attempts |
| `EV_NOTIFY_DEDUP_WINDOW_SECONDS` | `3600` | seconds | Duplicate-suppression window for identical fingerprints |
| `EV_NOTIFY_EMERGENCY_PRIORITY_THRESHOLD` | `0.7` | float 0–1 | Priority at/above which a notification is an emergency |
| `EV_NOTIFY_DIGEST_ENABLED` | `true` | boolean | Quiet-hours digest batching |
| `EV_NOTIFY_BOOT_BEACON` | `true` | boolean | One "EVIE is alive" notification per boot (no terminal needed) |
| `EV_NOTIFY_DEVICE_ROUTING` | `true` | boolean | Route notifications/life actions to the best reachable trusted device |

The quiet-hours window is `EV_QUIET_HOURS_START`/`EV_QUIET_HOURS_END` and the
per-day cap is `EV_DAILY_ALERT_BUDGET`; both predate Agent 14 and remain the
single source of truth.

## Agent OPENCODE — chat via the local `opencode` server (additive)

`EV_CHAT_PROVIDER=opencode` routes reasoning through a headless
`opencode serve` instance, which reaches hosted models with the owner's
`OPENCODE_API_KEY` (no separate DeepSeek key needed). The server is **session
based and not OpenAI-compatible**: there are no `/v1` routes. See
`docs/OPENCODE.md` for the transport, cost measurements and known limits.

| Key | Default | Values | Purpose |
| --- | --- | --- | --- |
| `EV_OPENCODE_BASE_URL` | `http://localhost:4096` | URL | Headless `opencode serve` address (localhost only; the server has no auth by default) |
| `EV_OPENCODE_PROVIDER_ID` | `opencode-go` | string | opencode provider id (`opencode models` lists them) |
| `EV_OPENCODE_MODEL` | `deepseek-v4-flash` | string | Model id within that provider |
| `EV_OPENCODE_AGENT` | `ev-minimal` | string | opencode agent to run. EV ships `.opencode/agents/ev-minimal.md` (no tools, one-line prompt): built-in agents add 6.7k–14.7k preamble tokens per call, `ev-minimal` adds ~200 |
| `EV_OPENCODE_AGENT_TEMPERATURE` | `0.7` | float | Mirror of the temperature declared in the agent markdown — the session API has no temperature field, so EV cannot set it per request |
| `EV_OPENCODE_SESSION_REUSE` | `false` | boolean | `false` = one ephemeral session per request, deleted afterwards, so opencode keeps no conversation memory. `true` = sticky session that accumulates history and cost |
| `EV_OPENCODE_SESSION_TITLE` | `ev` | string | Title used for EV's ephemeral sessions (makes leaks visible in `opencode session`) |
| `EV_OPENCODE_API_KEY` | — | string | Optional EV-side copy of the credential; the **server** process needs its own |
| `EV_OPENCODE_ENV_FILE` | `~/.config/ev/opencode.env` | path | Operator env file holding `OPENCODE_API_KEY`, also sourced by `launchd/ev.opencode.plist` (launchd never reads `~/.zshrc`) |
| `EV_OPENCODE_REQUIRE_API_KEY` | `true` | boolean | Fail closed when no credential is visible to EV. Set `false` only when the server holds the key somewhere EV cannot read |
| `EV_OPENCODE_READ_TIMEOUT_SECONDS` | `180` | seconds | Read timeout floor for the model round trip (the shared `EV_MODEL_*` timeouts still apply to connect/write/pool) |
| `EV_OPENCODE_STREAM_TIMEOUT_SECONDS` | `300` | seconds | Hard deadline for one streamed turn before a typed `ProviderStreamError` |
| `EV_OPENCODE_TOOL_EMULATION` | `false` | boolean | `false` = no tools on this provider; `chat_with_tools` answers without them and marks the result degraded. `true` = structured-output emulation, still checked by the gateway's `validate_tool_calls` |
| `EV_OPENCODE_FORMAT_RETRIES` | `1` | int | opencode-side retries when the model breaks the structured-output schema |

`OPENCODE_API_KEY` itself is not an `EV_`-prefixed setting: it is the opencode
server's own credential and must be in the environment of the server process
(`.env`, `~/.config/ev/opencode.env`, or the interactive shell that starts
`opencode serve`).

# --- AGENT 2 FOUNDRY · VOICE ACTIVATION (append-only) ---

Pragmatic voice defaults for this Mac (Apple M2, 8 GB):

| Key | Recommended | Notes |
| --- | --- | --- |
| `EV_VOICE_ASR_PROVIDER` | `faster_whisper` | Proven locally; model downloads on first use. Parakeet is not pinned yet (streaming split export ≠ `ParakeetOnnxSession` contract). |
| `EV_VOICE_TTS_PROVIDER` | `openai_compat` | Production default per fleet law §13; local fallback `kokoro` after pulling `tts-kokoro-82m-int8` + `tts-kokoro-voices-v1.0`. |
| `EV_ALLOW_REMOTE_TTS` | `true` | Required for the openai_compat default; fail-closed gate. |
| `EV_VOICE_WAKE_PROVIDER` | `openwakeword` | Package ships in the `ml` extra; the custom EVIE head is Agent 3's deliverable. |
| `EV_VOICEPRINT_PROVIDER` | `campp` | Weights need Agent 5's raw-waveform CAM++ ONNX export; fails closed until then. |

One-time commands:

```bash
cd backend && uv sync --extra ml --extra face --extra dev
uv run python -m app.ml.cli pull tts-kokoro-82m-int8 tts-kokoro-voices-v1.0
export EV_VOICE_ASR_PROVIDER=faster_whisper
export EV_VOICE_TTS_PROVIDER=openai_compat EV_ALLOW_REMOTE_TTS=true
```

`make voice-deps`, `make model-pull-voice`, and `make voice-preflight` wrap
the same steps; `make voice-preflight` prints Foundry's per-engine readiness
and remediation (the existing `make preflight` is Agent 20's report).

# --- AGENT 12 CONDUIT · WAVE LIFE (append-only) -------------------------------

Apple life bridges (Messages / Contacts / Phone / FaceTime / Mail via
EVLifeHelper, and the iPhone device-proxy queue). Everything fails loudly when
the helper is missing; local mode never fakes a sent message or placed call.

| Key | Default | Values | Purpose |
| --- | --- | --- | --- |
| `EV_LIFE_HELPER_PATH` | — | path | Executable `EVLifeHelper` binary (Agent 18/SUIT). Empty = life actions fail closed. |
| `EV_MESSAGING_PROVIDER` | `local` | `local` \| `macos_life` \| `device_proxy` | Provider used by runtime/notify when dispatching `send_message`. |
| `EV_LIFE_AUTONOMY` | `default` | `default` \| `full` | `full` = owner opted out of per-action confirmation inside granted scopes. |
| `EV_LIFE_CONTACT_ALLOWLIST` | `all` | `all` \| `starred` \| `any` | Which recipients are pre-authorized once the standing scope is granted. |
| `EV_LIFE_CONFIRM_UNKNOWN` | `true` | boolean | Require `confirm: true` (or approval) for recipients outside the allowlist. |
| `EV_LIFE_HELPER_TIMEOUT_SECONDS` | `20` | seconds | Helper subprocess hard timeout. |
| `EV_LIFE_HELPER_MAX_OUTPUT_BYTES` | `65536` | bytes | Helper stdout cap; oversized output is a loud failure. |

Per-integration config overrides (non-secret): `helper_path`, `contact_allowlist`,
`autonomy`, `confirm_unknown` inside `config` when installing the integration.

# --- EV VOICE CONTROL PLAN (append-only, 2026) --------------------------------

Live speech surface modes for the realtime brain (OpenAI Realtime
`gpt-realtime-2.1-mini` / Grok Voice). See `docs/VOICE_CONTROL_PLAN.md`.

| Key | Default | Values | Purpose |
| --- | --- | --- | --- |
| `EV_VOICE_LIVE_MODE` | `supervised` | `supervised` \| `shadow` \| `autonomous` | `supervised` = full curated surface (historical behavior, unchanged). `shadow` = UI verbs + `recall_history` + generic capabilities; history is injected read-only as a `SHADOW MEMORY` block per owner turn (no function call needed to answer the past). `autonomous` = zero tools, pure speech-to-speech chat (no memory, no actions). |
| `EV_VOICE_SHADOW_K` | `5` | 1–10 | Memory chunks injected per owner turn in `shadow` mode. |
| `EV_VOICE_SHADOW_BUDGET_TOKENS` | `900` | ≥64 | Token cap for the injected `SHADOW MEMORY` block. |
| `EV_VOICE_SHADOW_MIN_SCORE` | `0.0` | 0–1 | Retrieval floor for shadow chunks. |
| `EV_UI_VERB_TOOLS_ENABLED` | `true` | boolean | Kill-switch for the `read/see/click/double_click/right_click/type/paste/key/scroll/drag` UI verbs. |
| `EV_MODEL_SURFACE_V2` | `legacy` | `legacy` \| `shadow` \| `on` | Existing F4 surface reducer (independent of `EV_VOICE_LIVE_MODE`); `on` reduces the projected surface to `F4_TARGET_SURFACE`. |
