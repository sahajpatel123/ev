from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EV_", env_file=".env", extra="ignore")

    app_name: str = "EV"
    environment: str = "dev"  # dev | test | prod

    # Identity is configuration, not provider-specific: swapping the model must
    # never change who EV is or how EV behaves.
    persona_name: str = "EV"
    persona_description: str = "the user's persistent personal AI companion"

    # Database. Defaults to SQLite for zero-setup dev/tests; compose uses Postgres.
    database_url: str = "sqlite+aiosqlite:///./ev.db"
    redis_url: str = "redis://localhost:6379/0"

    # Auth
    master_key: str = "ev-local-dev-key"

    # Ingestion pipeline
    processing_mode: str = "sync"  # sync | queue

    # Embeddings
    embedding_provider: str = "hash"  # hash | http
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 384

    # Voiceprint enrollment (consent-gated biometric data)
    voiceprint_provider: str = "hash"  # hash | http
    voiceprint_base_url: str | None = None
    voiceprint_api_key: str | None = None
    voiceprint_model: str = "ecapa-tdnn-voxceleb"
    voiceprint_dim: int = 192
    voiceprint_threshold: float = 0.72

    # Chat gateway
    chat_provider: str = "echo"  # echo | mock | deepseek
    model_call_log_enabled: bool = True
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_api_key: str | None = None
    deepseek_model: str = "deepseek-v4-flash-0731"

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

    # 24/7 runtime state machine
    runtime_verify_timeout_seconds: int = 15
    runtime_awake_timeout_seconds: int = 120
    runtime_processing_timeout_seconds: int = 90
    runtime_respond_timeout_seconds: int = 60
    runtime_followup_timeout_seconds: int = 30
    runtime_heartbeat_grace_seconds: int = 300
    runtime_dlq_max_attempts: int = 3
    runtime_urgent_priority_threshold: float = 0.7

    # Object storage
    object_store_backend: str = "local"  # local | s3
    storage_root: str = "./storage"
    s3_endpoint_url: str | None = None
    s3_bucket: str = "ev"
    s3_access_key: str | None = None
    s3_secret_key: str | None = None

    # Integrations & ecosystem
    # Separate key for encrypting integration credentials. If empty, EV derives
    # one from the master key (single-user convenience); set this to a dedicated
    # random key so vault access does not share the master-key trust domain.
    vault_key: str = ""
    webhook_max_skew_seconds: int = 300
    webhook_rate_limit: int = 60
    webhook_window_seconds: int = 60
    plugin_timeout_seconds: int = 3
    plugin_max_output_bytes: int = 65_536

    # Misc
    access_log_enabled: bool = True
    cors_origins: list[str] = ["*"]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
