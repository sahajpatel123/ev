from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _load_production_secret_overlay() -> None:
    """P0.1 MASTER SECRET ISOLATION (PART 31-35).

    Production secrets live OUTSIDE the repository in
    ``~/.ev/secrets/production.env`` (chmod 0600). They are injected into
    the process environment here, BEFORE pydantic reads .env, so that:

    - runtime services keep working with no secret in the repo;
    - general development agents reading the repository .env can no longer
      obtain the production master credential.

    Keys already present in os.environ win (explicit operator override).
    """
    import os as _os
    from pathlib import Path as _Path

    overlay = _Path(
        _os.environ.get("EV_SECRETS_FILE", "~/.ev/secrets/production.env")
    ).expanduser()
    try:
        if not overlay.exists():
            return
        for line in overlay.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if key and key not in _os.environ:
                _os.environ[key] = value
    except Exception:
        # Never crash configuration on secret-overlay problems; explicit
        # env vars and .env still apply.
        pass


_load_production_secret_overlay()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EV_", env_file=".env", extra="ignore")

    app_name: str = "EV"
    environment: str = "dev"  # dev | test | prod

    # Identity is configuration, not provider-specific: swapping the model must
    # never change who EV is or how EV behaves.
    persona_name: str = "EV"
    persona_description: str = (
        "the owner's personal AI — house, phone, workshop, and visor"
    )

    # Database. Defaults to SQLite for zero-setup dev/tests; compose uses Postgres.
    database_url: str = "sqlite+aiosqlite:///./ev.db"
    redis_url: str = "redis://localhost:6379/0"

    # Auth
    master_key: str = "ev-local-dev-key"

    # Ingestion pipeline
    processing_mode: str = "sync"  # sync | queue

    # Memory OS: Postgres is authority. OpenAI Realtime sessions are disposable.
    # Default gate is off so live voice latency stays frozen. shadow = measure only.
    memory_gate: str = "off"  # off | shadow | on
    # F2 computer executor: off | shadow | on. off = legacy paths only.
    # shadow = plan/validate/risk only (mutations never double-execute).
    # on = existing tool names route through the internal executor.
    computer_executor_v2: str = "off"
    memory_dir: str | None = None  # default ~/Library/Application Support/EV/memory
    memory_curator_enabled: bool = True
    memory_curator_version: str = "1.1"
    memory_curator_batch_events: int = 8
    memory_bootstrap_max_tokens: int = 1400
    memory_prefetch: str = "off"  # off | shadow | on — never injects

    # Cross-platform Device Gateway / PWA. Production Memory OS stays off.
    cross_platform_enabled: bool = True
    cross_platform_production_memory: bool = False
    device_protocol_version: str = "1"
    native_actions_enabled: bool = True
    native_broker_version: str = "1.0.0"
    pwa_build: str = "2026.08.22.23"
    phone_audio_backend: str = "webrtc_strict"  # webrtc_strict | webrtc | pcm_ws | encoded | auto
    phone_asr_model: str = "gpt-4o-transcribe"
    phone_asr_language: str = "en"
    phone_input_noise_reduction: str = "near_field"  # near_field | far_field | off
    pwa_design_version: str = "veil-1"
    device_access_ttl_seconds: int = 900
    pairing_ttl_seconds: int = 900
    conversation_lease_ttl_seconds: int = 45
    active_conversation_ttl_seconds: int = 3600
    sandbox_namespace: str = "cross_platform_test"
    home_station_mode: bool = True
    home_station_keep_awake_on_ac: bool = True
    home_station_keep_awake_on_battery: bool = False
    tailscale_serve_apply: bool = False

    # Embeddings
    embedding_provider: str = "hash"  # hash | http
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 384
    # Send the OpenAI-compatible "dimensions" parameter so hosted models like
    # text-embedding-3-small return EV_EMBEDDING_DIM directly. A hosted model
    # that ignores it and returns another dimension fails loudly (never
    # silently truncated).
    embedding_http_dimensions: bool = True
    # --- AGENT 8 SYNAPSE (embeddings/retrieval) ---
    # ONNX execution provider for local embeddings ("auto" picks CPU; a
    # comma-separated list such as "CoreMLExecutionProvider,CPUExecutionProvider"
    # tries Apple's accelerator first).
    embedding_onnx_provider: str = "auto"
    # Optional on-demand cross-encoder rerank, hard queries only. The base
    # weighted formula is never changed; rerank is an explicit post-pass.
    reranker_enabled: bool = True
    reranker_hard_threshold: float = 0.55
    reranker_span_threshold: float = 0.05
    reranker_candidates: int = 50
    reranker_final_k: int = 10
    reranker_batch_size: int = 8
    # Per-query min-max calibration of the semantic component across the
    # candidate pool. Modern embedding models (granite R2) produce compressed
    # raw cosines (~0.7-0.9 for both related and unrelated text); rescaling
    # restores the discriminative signal before the locked weights apply.
    # The raw cosine is always exposed as "semantic_raw" in components.
    semantic_normalize: bool = True
    # --- END AGENT 8 SYNAPSE ---

    # Web research (plan 11.3 / D-03): none = memory-only; mock for tests;
    # brave uses a user-supplied Brave Search API key; live uses Open-Meteo
    # (weather, no key) plus Wikipedia/DuckDuckGo, and Brave when a key exists.
    search_provider: str = "none"  # none | mock | brave | live
    brave_search_base_url: str = "https://api.search.brave.com/res/v1/web/search"
    brave_search_api_key: str | None = None
    search_timeout_seconds: float = 10.0
    search_result_limit: int = 5
    location_place: str | None = None
    location_file: str = "~/.ev/location.json"
    home_lat: float | None = None
    home_lon: float | None = None
    home_geofence_m: float = 80.0

    # Local vision/OCR (Domain 15): deterministic is the zero-dependency
    # default; tesseract enables real OCR when the binary is installed.
    vision_provider: str = "deterministic"  # deterministic | tesseract | apple_vision | deepseek_ocr
    vision_tesseract_binary: str = "tesseract"
    # Hard cap on any single media part crossing the model boundary.
    max_media_bytes: int = 10 * 1024 * 1024

    # Voiceprint enrollment (consent-gated biometric data). hash = deterministic
    # dev/test embeddings; speechbrain = local ECAPA-TDNN (spkrec-ecapa-voxceleb);
    # http = remote encoder service (requires EV_ALLOW_REMOTE_VOICEPRINT_PROCESSING).
    voiceprint_provider: str = "hash"  # hash | speechbrain | http
    voiceprint_base_url: str | None = None
    voiceprint_api_key: str | None = None
    voiceprint_model: str = "speechbrain/spkrec-ecapa-voxceleb"
    voiceprint_model_dir: str | None = None  # SpeechBrain cache/download dir
    voiceprint_dim: int = 192
    voiceprint_threshold: float = 0.72
    # Wake clips are often <1s of "EVIE"; far-field CAM++ on that is much
    # lower than the close-talk enrollment mean (~0.66).
    voiceprint_wake_threshold: float = 0.45

    # Voice wake word. phrase = deterministic text-hint/frame matcher (dev/test);
    # porcupine = Picovoice Porcupine (custom "EVIE" .ppn model or built-in keyword);
    # silero_vad = Silero VAD gate layered over a local keyword engine.
    voice_wake_provider: str = "phrase"  # phrase | porcupine | silero_vad
    voice_wake_access_key: str | None = None  # Picovoice access key
    voice_wake_model_path: str | None = None  # custom "EVIE" .ppn file
    voice_wake_porcupine_library_path: str | None = None
    voice_wake_sensitivity: float = 0.6
    voice_wake_vad_model_path: str | None = None  # Silero JIT model path
    voice_wake_vad_threshold: float = 0.5

    # --- AGENT 7 ROSTER (append-only) ---------------------------------------
    # Consented face enrollment + recognition. hash = deterministic dev/test
    # embeddings; sface = OpenCV Zoo SFace ONNX (Apache-2.0, on_demand).
    face_provider: str = "hash"  # hash | sface
    face_model_path: str | None = None  # explicit .onnx path; None = ml cache
    face_embedding_dim: int = 512
    # Placeholder until ROC calibration replaces it; never silently tuned.
    face_threshold: float = 0.55
    face_min_photos: int = 5
    face_quality_floor: float = 0.5
    face_confidence_floor: float = 0.6
    # Public-figure biodata (Wikidata SPARQL + Wikipedia REST), attributed cache.
    biodata_provider: str = "wikidata"  # wikidata | none
    biodata_ttl_seconds: int = 7 * 24 * 3600
    biodata_wikidata_sparql_url: str = "https://query.wikidata.org/sparql"
    biodata_wikipedia_rest_url: str = "https://en.wikipedia.org/api/rest_v1/page/summary"
    biodata_timeout_seconds: float = 12.0

    # --- AGENT 3 EARS (append-only) ------------------------------------------
    # Always-on microphone capture + VAD + wake + scene (see docs/AUDIO.md).
    # openwakeword = custom "EVIE" ONNX head trained by backend/clients/ears.
    voice_wake_openwakeword_model_path: str | None = None  # exported .onnx head
    voice_wake_openwakeword_verifier_path: str | None = None  # logistic weights
    voice_wake_openwakeword_threshold: float = 0.5
    voice_wake_openwakeword_verifier_threshold: float = 0.3

    # Microphone capture (PortAudio/sounddevice on macOS).
    ears_device: str | None = None  # device name or index; None = system default
    ears_sample_rate: int = 16000
    ears_ring_seconds: float = 10.0  # lock-free pre-roll ring capacity
    ears_block_ms: int = 20  # capture block size in milliseconds
    ears_device_id: str = "mac-ears"

    # VAD (Silero ONNX when present; energy/ZCR heuristic otherwise).
    ears_vad_model_path: str | None = None
    ears_vad_threshold: float = 0.5
    ears_vad_pre_roll_s: float = 0.4
    ears_vad_post_roll_s: float = 0.6
    ears_vad_min_speech_s: float = 0.12
    ears_max_segment_s: float = 60.0  # utterance cap → bounded memory
    # After wake, follow-up commands are short. A 60s listening clip cannot
    # finish uploading before a 30s HTTP client timeout.
    ears_listen_max_segment_s: float = 20.0
    ears_http_timeout_s: float = 45.0
    # Idle wake spotting: keep a short rolling clip so "Hey EVIE" is not split,
    # and skip room-tone so Whisper is free when the name is actually spoken.
    # Idle wake spotting: a short rolling clip so "Evie" is scored quickly
    # instead of blocking a 2.5–60 s Whisper job that swallows repeats.
    ears_wake_chunk_s: float = 1.2
    ears_idle_min_rms: float = 140.0
    ears_idle_min_peak: int = 600

    # Wake threshold used by the ears process (tuned against ambient audio).
    ears_wake_threshold: float = 0.5

    # On-device wake fallback when the custom openWakeWord head is not on disk.
    # ``ears_wake_local_spotter`` runs a small local faster-whisper model
    # (``ears_wake_asr_model``) in the ears process so the wake word works
    # without a trained head and without shipping every VAD clip to the API.
    ears_wake_local_spotter: bool = True
    ears_wake_asr_model: str = "tiny"  # dedicated fast wake model, not the ASR model

    # Stuck-mic / self-echo loops: drop segments whose content literally
    # repeats (docs/VOICE.md §0) before they reach ASR or the API.
    ears_stuck_loop_drop: bool = True
    ears_stuck_loop_threshold: float = 0.10

    # Stream TTS chunks back over SSE and play each sentence as it arrives,
    # instead of waiting for the full reply (docs/VOICE.md §6).
    ears_stream_playback: bool = True

    # Prefer the full-duplex WS /v1/voice/live door over per-utterance SSE
    # once a voice session is awake (docs/LIVE_VOICE.md §2).
    ears_live_enabled: bool = True

    # Audio-scene (YAMNet ONNX when present; VAD-feature fallback otherwise).
    ears_scene_model_path: str | None = None
    ears_scene_labels_path: str | None = None

    # Diarization (on-demand, meeting recordings only). Consent defaults to
    # false; the HF token is used only for the selected recording.
    ears_diarize_consent: bool = False
    ears_diarize_hf_token: str | None = None

    # Paths for the acceptance-gate datasets (human-provided, owner-consented).
    ears_data_wake_dir: str | None = None
    ears_data_ambient_path: str | None = None
    ears_data_vad_labels: str | None = None
    ears_data_scene_labels: str | None = None

    # Delivery + privacy. Raw audio is never persisted by default and only
    # reaches Agent 4 when EV_EARS_CONSENT=true and an API URL is configured.
    ears_api_url: str | None = None
    ears_api_key: str | None = None
    ears_consent: bool = False
    ears_dry_run: bool = False
    ears_save_segments_dir: str | None = None  # explicit opt-in debug dump
    ears_report_interval_s: float = 300.0

    # Voice ASR (speech-to-text). echo = offline transcript hints (dev/test);
    # openai_compat = any OpenAI-compatible /audio/transcriptions endpoint.
    # faster_whisper = local Whisper-class transcription (faster-whisper).
    voice_asr_provider: str = "echo"  # echo | openai_compat | faster_whisper
    voice_asr_base_url: str | None = None
    voice_asr_api_key: str | None = None
    voice_asr_model: str = "whisper-1"
    voice_asr_model_dir: str | None = None  # faster-whisper model cache/download dir
    voice_asr_device: str = "auto"  # auto | cpu | cuda
    voice_asr_compute_type: str = "auto"  # auto | int8 | float16 | float32
    voice_asr_language: str | None = None  # default language; per-request overrides
    voice_asr_vad_filter: bool = True
    # Wake-mode transcription keeps Whisper's default no-speech gate so real
    # short wake clips are transcribed instead of suppressed (0.1 suppressed
    # measured "EVIE" takes). Weak wake aliases are gated on the per-segment
    # no_speech_prob rather than on this suppression threshold.
    voice_asr_wake_no_speech_threshold: float = 0.6

    # Voice TTS (natural speech with urgency/warmth/brevity controls).
    # meta = offline SSML metadata (dev/test); openai_compat = any
    # OpenAI-compatible /audio/speech endpoint.
    # piper = local Piper neural TTS (ONNX voice).
    voice_tts_provider: str = "meta"  # meta | openai_compat | piper
    # TURN AUTHORITY V2 canary (reversible): VAD becomes a sensor; the
    # application explicitly creates responses after a logical owner turn
    # yields (speech_stopped + bounded grace, no continuation).
    turn_authority_v2_enabled: bool = False
    # G1.4 Authoritative Turn Gate — backend owns response creation.
    # Every final owner turn passes through TurnGate; provider VAD is sensor only.
    turn_gate_enabled: bool = False
    # TEST-ONLY diagnostic: per-response instructions that force ONE
    # uninterrupted long provider response, used solely to recreate the
    # self-hearing failure envelope in internal verification. Never changes
    # normal owner conversations. Opt-in, off by default, loudly logged.
    long_form_diagnostic: bool = False
    voice_tts_base_url: str | None = None
    voice_tts_api_key: str | None = None
    voice_tts_model: str = "gpt-4o-mini-tts"
    voice_tts_voice: str = "alloy"
    voice_tts_rate: float = 1.0
    voice_tts_format: str = "mp3"
    voice_tts_model_dir: str | None = None  # Piper voice model directory
    voice_tts_binary: str = "piper"
    voice_tts_length_scale: float = 1.0
    voice_tts_noise_scale: float = 0.667
    voice_tts_sentence_silence: float = 0.2

    # --- AGENT 4 VOICE — real on-device engines (additive) ---
    # Canonical engine identifiers for the parakeet/kokoro providers. These
    # are the reconciled defaults for the production engines; the legacy
    # voice_asr_model / voice_tts_model values above remain for
    # openai_compat/piper only.
    voice_asr_engine: str = "parakeet-eou-120m-int8"
    voice_asr_alt_engine: str = "parakeet-tdt-v3-int8"  # opt-in accuracy tier
    voice_asr_onnx_path: str | None = None  # explicit Parakeet .onnx path
    voice_asr_stream_chunk_ms: int = 200  # streaming partial cadence (<=300 ms first partial)
    voice_asr_allowed_roots: list[str] | None = None  # allowlisted audio_ref dirs
    voice_tts_engine: str = "kokoro-82m-fp16"
    voice_tts_kokoro_voice: str = "bf_alice"
    voice_tts_say_voice: str = "Ava"
    # Edge neural TTS (free, no key, remote). Natural voices; the warm British
    # female is the closest public match to the movie E.V. profile. Requires
    # EV_ALLOW_REMOTE_TTS=true. Voice consistency is enforced by NOT falling
    # back to a different engine: on remote failure the reply is text-only.
    voice_tts_edge_voice: str = "en-GB-SoniaNeural"
    voice_tts_retries: int = 2  # transient retries for the remote TTS call
    voice_chatterbox_engine: str = "chatterbox-nano"  # opt-in expressive tier
    voice_chatterbox_voice: str = "default"
    # --- AGENT 4 VOICE — Wave Life session continuity (additive) ---
    # One wake opens a verified session; the owner stays in ACTIVE/FOLLOW_UP
    # until a sleep phrase, explicit end, or long true idle.
    voice_follow_up_seconds: int = 240  # follow-up window (180-300 recommended)
    voice_session_timeout_seconds: int = 900  # long-idle lock (must exceed follow-up)
    voice_verify_timeout_seconds: int = 20  # one-time verify window after wake
    voice_continuity_conversation_id: str = "CONTINUITY_LIVE"  # one lifelong thread
    voice_addressivity_enabled: bool = True  # VAD + owner verify per utterance
    voice_addressivity_vad_threshold: float = 0.5
    # Push-to-talk / utterance bounds so the menu bar cannot sit on ".thinking."
    # until URLSession's default 60s timeout. Clip audio, cap ASR, cap chat+TTS.
    voice_utterance_max_seconds: float = 120.0
    voice_asr_timeout_seconds: float = 120.0
    voice_turn_timeout_seconds: float = 300.0
    voice_tts_timeout_seconds: float = 60.0
    voice_sleep_phrases: list[str] = Field(
        default_factory=lambda: [
            "that's all",
            "that's it",
            "go to sleep",
            "stop listening",
            "stop evie",
            "goodbye evie",
            "never mind",
        ]
    )
    # EV LIVE — full-duplex conversational runtime (docs/LIVE_VOICE.md).
    # Additive: the HTTP utterance path stays; WS /v1/voice/live is the
    # continuous loop. Knobs map 1:1 onto TurnTakingConfig / BackchannelPolicy.
    voice_live_enabled: bool = True
    voice_live_tick_ms: int = 50
    voice_live_end_pause_ms: int = 280
    voice_live_thinking_grace_ms: int = 700
    voice_live_trailing_grace_ms: int = 1100
    voice_live_wake_hold_ms: int = 650
    voice_live_min_speech_ms: int = 160
    voice_live_quiet_end_pause_ms: int = 1300
    voice_live_response_cooldown_ms: int = 450
    voice_live_max_pause_ms: int = 2500
    # OWNER DECISION 2026-08-23: server listener backchannel cues are
    # CANCELLED. Kept only as a quarantined legacy knob; the engine no longer
    # produces cues regardless of this value.
    voice_live_backchannel: bool = False
    voice_live_vad_threshold: float = 0.35
    voice_live_asr_partial_ms: int = 160

    # Chat gateway
    chat_provider: str = "echo"  # echo | mock | deepseek | xai | local
    local_model_base_url: str | None = None  # OpenAI-compatible local server (Ollama/llama.cpp)
    local_model_name: str = "llama3"
    model_call_log_enabled: bool = True
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_api_key: str | None = None
    # Official API id (not the Hugging Face checkpoint name). Serves the current
    # Flash build. Thinking is off by default so voice replies stay spoken-speed.
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_thinking: bool = False
    # Official xAI / SpaceXAI. Typed chat uses Grok 4.6. Live conversation can
    # use Grok Voice Think Fast 2.0 (speech-to-speech realtime), which is not
    # a chat-completions model — see EV_VOICE_LIVE_BRAIN / EV_XAI_VOICE_MODEL.
    xai_base_url: str = "https://api.x.ai/v1"
    xai_api_key: str | None = None
    xai_model: str = "grok-4.6"
    xai_voice_model: str = "grok-voice-think-fast-2.0"
    xai_voice_voice: str = "eve"
    xai_voice_realtime_url: str = "wss://api.x.ai/v1/realtime"
    xai_voice_vad_threshold: float = 0.72
    xai_voice_silence_ms: int = 550
    # auto = OpenAI Realtime if EV_OPENAI_API_KEY is set, else Grok Voice if
    # EV_XAI_API_KEY is set; openai / xai = force; pipeline = local ASR+chat+TTS.
    voice_live_brain: str = "auto"
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_realtime_model: str = "gpt-realtime-2.1-mini"
    # OpenAI Realtime voice for spoken output (marin is the natural default).
    openai_realtime_voice: str = "marin"
    openai_realtime_url: str = "wss://api.openai.com/v1/realtime"
    # G1.3/G1.5 Turn Control Plane: Luna (GPT-5.6 Luna) via OpenAI Responses
    # structured outputs.  Primary is Luna, fallback is explicit.
    turn_control_provider: str = "openai"
    turn_control_model: str = "gpt-5.6-luna"
    turn_control_fallback_model: str = "gpt-4o-mini"
    openai_chat_model: str | None = None  # alias for turn_control_model when set

    # Intelligence filter: optional provider-backed critic (staged trust).
    filter_critic_enabled: bool = False
    filter_critic_max_iterations: int = 2
    filter_critic_modes: tuple[str, ...] = ("coaching", "emergency", "analytical")

    # --- AGENT 16 CONSCIENCE ---
    # Optional on-demand NLI critic (MobileBERT MNLI q8, ~26 MB on disk). It
    # scores extracted claims as entailed / neutral / contradicted against the
    # memories actually in context. The critic is evicted after every audit so
    # it is never resident during a voice session; offline CI (no weights)
    # degrades deterministically to the lexical grounding path.
    nli_critic_enabled: bool = True
    nli_critic_evict_after_use: bool = True
    nli_critic_batch_size: int = 8
    # --- END AGENT 16 CONSCIENCE ---

    # Weight training (Domain 7): explicit provider boundary for adapter
    # fine-tuning. local-lora runs a configured command against the exported
    # JSONL; openai-fine-tune uploads the file and creates a hosted job.
    training_provider: str = "local-lora"  # local-lora | openai-fine-tune
    training_local_cmd: str | None = None
    training_openai_api_key: str | None = None
    training_openai_base_url: str = "https://api.openai.com/v1"
    training_openai_model: str | None = None

    # Orchestrator
    context_budget_tokens: int = 20_000
    max_retrieval_memories: int = 50

    # Single conversation context assembly (rolling summary + progressive depth)
    rollup_budget_tokens: int = 1_500
    standard_history_turns: int = 10
    deep_history_turns: int = 40
    deepest_history_turns: int = 150
    deep_retrieval_memories: int = 100
    deepest_retrieval_memories: int = 150

    # Attention budget / quiet hours
    quiet_hours_start: str = "22:00"
    quiet_hours_end: str = "08:00"
    daily_alert_budget: int = 5
    # IANA timezone for quiet-hours clock. Empty/invalid fails closed (treat as quiet).
    timezone: str | None = "UTC"
    # Companion: social-turn budget before an isolation scan may fire a nudge.
    social_nudge_after_turns: int = 5
    # Workshop printer; unset appears as needs_setup on the protocol sheet.
    octoprint_url: str | None = None

    # 24/7 runtime state machine
    runtime_verify_timeout_seconds: int = 15
    runtime_awake_timeout_seconds: int = 120
    runtime_processing_timeout_seconds: int = 90
    runtime_respond_timeout_seconds: int = 60
    runtime_followup_timeout_seconds: int = 30  # REST hint only; not a session door
    runtime_session_timeout_seconds: int = 900  # long-idle lock for listening states
    runtime_heartbeat_grace_seconds: int = 300
    runtime_dlq_max_attempts: int = 3
    runtime_urgent_priority_threshold: float = 0.7
    runtime_focus_urgent_threshold: float = 0.85
    runtime_daemon_tick_seconds: int = 30

    # Routines & automations
    scheduler_tick_seconds: int = 60
    scheduler_max_days_lookahead: int = 366
    automation_failure_threshold: int = 3

    # Live data retention: the raw live stream is append-only; retention deletes
    # only *consumed* events past the window, never the latest event of a
    # channel or events still linked as provenance.
    live_event_retention_days: int = 90
    live_retention_keep_latest: bool = True
    # Scheduler cadence for live-data maintenance. Retention is deliberately
    # rare (once a day) because it physically deletes raw rows; rebuild is
    # cheap and keeps the derived layer fresh after every batch of events.
    live_retention_interval_seconds: int = 86_400
    live_rebuild_interval_seconds: int = 3_600

    # Object storage
    object_store_backend: str = "local"  # local | s3
    storage_root: str = "./storage"
    s3_endpoint_url: str | None = None
    s3_bucket: str = "ev"
    s3_access_key: str | None = None
    s3_secret_key: str | None = None

    # Retention / maintenance
    tombstone_blob_retention_days: int = 30
    backup_retention_count: int = 7
    backup_passphrase: str | None = None

    # Integrations & ecosystem
    # Required: dedicated key for encrypting integration OAuth tokens and
    # webhook secrets. EV never derives it from the master key, so a leaked
    # master key cannot decrypt the credential vault (and vice versa).
    # The empty default is intentional: pydantic still validates min_length,
    # so Settings() fails closed at startup unless EV_VAULT_KEY is provided.
    vault_key: str = Field(default="", min_length=16)
    webhook_max_skew_seconds: int = 300
    webhook_rate_limit: int = 60
    webhook_window_seconds: int = 60
    webhook_max_body_bytes: int = 1_048_576
    plugin_timeout_seconds: int = 3
    plugin_max_output_bytes: int = 65_536

    # Misc
    access_log_enabled: bool = True
    cors_origins: list[str] = ["*"]

    # --- AGENT 19 VAULT (append-only) --------------------------------------
    # WebAuthn ceremony parameters. RP ID must match the origin's effective
    # domain; origins is an explicit allowlist (never "*").
    webauthn_rp_id: str = "localhost"
    webauthn_rp_name: str = "EV"
    webauthn_origins: list[str] = [
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ]
    webauthn_challenge_ttl_seconds: int = 300
    # When true, registration requires an attestation statement that verifies
    # cryptographically AND chains to a configured trust root. With no trust
    # roots configured this fails closed: only "none" attestations would be
    # rejected as non-attested, so an operator must configure roots explicitly.
    webauthn_require_attestation: bool = False
    # Optional PEM trust anchors (list of PEM strings or file paths) used to
    # verify packed/fido-u2f attestation certificate chains.
    webauthn_attestation_trust_roots_pem: list[str] = []

    # --- AGENT 10 CORTEX (shared health) ------------------------------------
    # Shared append-only repair: Agents 12/14 appended the fields below outside
    # the Settings class, so pydantic never saw them and any module importing
    # them (integrations/oauth, workers/notify, ...) crashed at import time.
    # Additive duplicates are intentional until those agents reconcile their
    # blocks; defaults mirror their module-level declarations.
    oauth_state_ttl_seconds: int = 600
    oauth_http_timeout_seconds: float = 15.0
    calendar_sync_days: int = 14
    calendar_sync_max_events: int = 50
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: str | None = None
    google_oauth_redirect_uri: str | None = None
    github_oauth_client_id: str | None = None
    github_oauth_client_secret: str | None = None
    github_oauth_redirect_uri: str | None = None
    notify_backend: str = "console"
    notify_macos_helper_path: str | None = None
    notify_macos_bundle_id: str = "ev.pulse"
    notify_macos_build_dir: str = "./storage/notify"
    notify_macos_allow_osascript: bool = True
    notify_webhook_url: str | None = None
    notify_webhook_secret: str | None = None
    notify_apns_enabled: bool = False
    notify_apns_key_id: str | None = None
    notify_apns_team_id: str | None = None
    notify_apns_topic: str | None = None
    notify_apns_key_path: str | None = None
    notify_max_attempts: int = 3
    notify_retry_interval_seconds: int = 300
    notify_dedup_window_seconds: int = 3600
    notify_emergency_priority_threshold: float = 0.7
    notify_digest_enabled: bool = True
    notify_boot_beacon: bool = True
    notify_device_routing: bool = True

    # --- AGENT 10 CORTEX (API-only reliability) -----------------------------
    # DeepSeek is the primary reasoning provider. These knobs make outages
    # degrade cleanly: explicit timeouts, bounded jittered retries, a circuit
    # breaker, and an enforceable monthly cost cap (default matches
    # app.ops.budgets.MONTHLY_COST_BUDGET_USD = $40).
    model_connect_timeout_seconds: float = 10.0
    model_read_timeout_seconds: float = 60.0
    model_write_timeout_seconds: float = 30.0
    model_pool_timeout_seconds: float = 10.0
    model_max_retries: int = 2
    model_retry_base_seconds: float = 0.5
    model_retry_max_seconds: float = 5.0
    circuit_failure_threshold: int = 3
    circuit_cooldown_seconds: float = 30.0
    circuit_half_open_success_threshold: int = 1
    monthly_cost_cap_usd: float = 40.0
    cost_cap_enabled: bool = True
    # Conservative completion projection used when refusing over-cap requests
    # before a provider call (actual usage is always measured after the call).
    model_estimated_max_completion_tokens: int = 4096

    # --- AGENT 10 CORTEX (life agency) ---------------------------------------
    # Standing owner authority for life actions (WAVE LIFE).
    # full            = no per-action approval inside granted standing scopes
    # confirm_unknown = known contacts act; unknown recipients need confirmation
    # confirm_all     = every life action requires explicit approval
    owner_autonomy: str = "full"

    # --- AGENT OPENCODE (append-only) ---------------------------------------
    # `opencode serve` as a chat provider (EV_CHAT_PROVIDER=opencode). The
    # server is session based, not OpenAI-compatible; see app/gateway/opencode.py.
    opencode_base_url: str = "http://localhost:4096"
    opencode_provider_id: str = "opencode-go"
    opencode_model: str = "deepseek-v4-flash"
    # EV's own minimal agent (.opencode/agents/ev-minimal.md): no tools and a
    # one-line prompt, because every built-in opencode agent injects a 6.7k–14.7k
    # token coding preamble into each request.
    opencode_agent: str = "ev-minimal"
    # Sampling lives in the agent definition — the session API has no
    # temperature field. Keep this in sync with the agent markdown.
    opencode_agent_temperature: float = 0.7
    # False = one ephemeral session per request, deleted afterwards, so opencode
    # holds no conversation memory and cannot inflate cost without bound.
    opencode_session_reuse: bool = False
    opencode_session_title: str = "ev"
    # Optional EV-side copy of the credential; the server needs its own.
    opencode_api_key: str | None = None
    # Also sourced by launchd/ev.opencode.plist (launchd never reads ~/.zshrc).
    opencode_env_file: str = "~/.config/ev/opencode.env"
    opencode_require_api_key: bool = True
    opencode_read_timeout_seconds: float = 180.0
    opencode_stream_timeout_seconds: float = 300.0
    # Structured-output emulation of tool calling (off by default: the session
    # API accepts no function definitions).
    opencode_tool_emulation: bool = True
    opencode_format_retries: int = 1
    # --- END AGENT OPENCODE ---

    # --- AGENT 12 CONDUIT (WAVE LIFE) -----------------------------------------
    # Real Apple-life bridges (Messages/Contacts/FaceTime/Mail) via Agent 18's
    # EVLifeHelper, plus the iPhone device-proxy actuator queue. The helper is
    # not merged yet; empty values keep offline CI green and every life action
    # fails loudly instead of pretending success.
    life_helper_path: str = ""
    messaging_provider: str = "local"  # local | macos_life | device_proxy
    life_autonomy: str = "default"  # default | full
    life_contact_allowlist: str = "all"  # all | starred | any
    life_confirm_unknown: bool = True
    life_helper_timeout_seconds: float = 20.0
    life_helper_max_output_bytes: int = 65_536
    # --- END AGENT 12 CONDUIT (WAVE LIFE) ---


@lru_cache
def get_settings() -> Settings:
    # vault_key has an empty default that fails min_length validation, so
    # Settings() alone (without EV_VAULT_KEY / .env) is not a complete config.
    return Settings()


settings = get_settings()

# ============================================================================
# SHARED APPEND-ONLY SECTION — docs/FLEET_LAW.md §3
# Additive only. Append inside YOUR block; never modify, reorder, reformat, or
# delete another agent's lines, settings, defaults, or signatures.
#
# --- AGENT 1 CONDUCTOR ---
# Reserved by Agent 1 (Conductor): fleet governance, integration, contract.
#
# --- AGENT 2 FOUNDRY ---
# --- AGENT 3 EARS ---
# --- AGENT 4 VOICE ---
# --- AGENT 5 SENTRY ---
# --- AGENT 6 EYES ---
# --- AGENT 7 ROSTER ---
# --- AGENT 8 SYNAPSE ---
# Settings fields live in the Settings class under the AGENT 8 marker above.
# --- END AGENT 8 SYNAPSE ---
# --- AGENT 9 MNEMO ---
# --- AGENT 10 CORTEX ---
# --- AGENT 11 FORGE ---
# --- AGENT 12 CONDUIT ---
# OAuth 2.0 authorization-code + PKCE for real provider integrations. Client
# ids/secrets are server-side environment secrets (never integration config);
# user access/refresh tokens live in the encrypted credential vault. The
# Settings fields live in the class above (AGENT 10 CORTEX shared-health
# reconciliation); empty defaults keep offline CI green and authorize/refresh
# fail closed with a clear message until the human sets provider credentials.
# --- AGENT 13 AMBIENT ---
# --- AGENT 14 PULSE ----------------------------------------------------------
# Notification delivery settings live in the Settings class above (Agent 10
# CORTEX reconciled the shared-health block; PULSE fields are under the
# AGENT 10 marker). This marker is reserved for Agent 14 additions.
# --- AGENT 15 ORACLE ---
# --- AGENT 16 CONSCIENCE ---
# Agent 16 settings fields live in the Settings class under the AGENT 16
# marker above. Dependency note for Agent 10 (CORTEX): chunk-level stream
# refinement lives in app/filter/stream_refiner.py; wire it into
# app/api/core.py::_stream_chat when adopting per-delta refinement.
# --- AGENT 17 WORKBENCH ---
# --- AGENT 18 SUIT ---
# --- AGENT 19 VAULT ---
# --- AGENT 20 LAUNCH ---
# ============================================================================
