from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openai_api_key: str = ""
    api_key: str = ""

    database_url: str = Field(default="", repr=False)
    redis_url: str = Field(default="", repr=False)
    auth_jwt_secret: str = Field(default="", repr=False)
    auth_jwt_issuer: str = "reciapp-api"
    auth_jwt_audience: str = "reciapp-ios"
    auth_access_token_ttl_seconds: int = 3600
    auth_refresh_token_ttl_seconds: int = 60 * 60 * 24 * 60
    cover_public_base_url: str = ""
    readiness_timeout_seconds: float = Field(default=3.0, ge=0.1, le=10)
    maintenance_mode: bool = False
    environment: str = "production"
    sentry_dsn: str = Field(default="", repr=False)
    sentry_environment: str = "production"
    sentry_release: str = ""
    sentry_traces_sample_rate: float = Field(default=0.0, ge=0.0, le=1.0)

    max_duration_seconds: int = 600
    transcribe_model: str = "gpt-4o-mini-transcribe"
    recipe_model: str = "gpt-6-luna"
    vision_model: str = "gpt-6-luna"
    job_ttl_seconds: int = 3600

    cost_transcribe_cents_per_min: float = 0.3
    cost_text_cents_per_extract: float = 0.1
    cost_ocr_cents_per_slide: float = 0.25
    # List prices used for real dashboard cost (USD per 1M tokens / per minute).
    openai_chat_input_usd_per_mtok: float = 0.10
    openai_chat_output_usd_per_mtok: float = 0.50
    openai_chat_cached_input_usd_per_mtok: float = 0.01
    openai_transcribe_input_usd_per_mtok: float = 1.25
    openai_transcribe_output_usd_per_mtok: float = 5.0
    openai_transcribe_usd_per_min: float = 0.003

    superwall_webhook_secret: str = ""
    superwall_application_id: int = 54783
    public_api_base_url: str = "https://51-255-43-100.sslip.io"

    dashboard_password: str = ""
    dashboard_session_secret: str = ""
    dashboard_totp_secret: str = ""
    dashboard_cookie_secure: bool = True

    cors_origins: str = "https://51-255-43-100.sslip.io"
    billing_guard_enabled: bool = True
    daily_api_budget_cents: float = 1000.0
    monthly_api_budget_cents: float = 5000.0
    user_monthly_budget_cents: float = 500.0
    max_job_cost_cents: float = 50.0
    # Skip paid OpenAI STT when local whisper already returned enough speech.
    local_stt_min_chars: int = 80
    # Deprecated: audio is gated by recipe completeness, not caption length.
    caption_skip_audio_min_chars: int = Field(default=80, ge=0, le=5000)
    # Hard ceiling on vision OCR frames per job (last resort).
    max_vision_frames: int = 8
    # First OCR spread pass size (was 4; 2 cuts CPU/RAM on last-resort vision).
    first_vision_pass_frames: int = Field(default=2, ge=1, le=8)
    ocr_slide_max_tokens: int = 400
    ocr_overlay_max_tokens: int = 800
    # Sized from real TikTok peak ~950MiB/job on 8GB host.
    max_concurrent_jobs: int = 8
    # Process slots (extract + translation). Serial extract starts are gated in
    # extract_recipe via user_has_processing_extract, not this counter alone.
    max_concurrent_jobs_per_user: int = 2
    max_pending_jobs_per_user: int = 20
    # Web requests use FastAPI BackgroundTasks by default. A separate worker
    # can be enabled after the durable lease migration is deployed.
    worker_enabled: bool = False
    worker_poll_seconds: float = 5.0
    worker_lease_seconds: int = 900
    worker_heartbeat_interval_seconds: int = Field(default=20, ge=5, le=60)
    worker_heartbeat_ttl_seconds: int = Field(default=90, ge=30, le=300)
    max_request_body_bytes: int = 262_144
    webhook_max_age_seconds: int = 7 * 24 * 60 * 60
    apple_root_ca_pem: str = ""
    apple_bundle_id: str = "com.membri.reciapp"
    apple_environment: str = "Production"
    apple_team_id: str = ""
    apple_key_id: str = ""
    apple_private_key: str = Field(default="", repr=False)
    apple_token_encryption_key: str = Field(default="", repr=False)
    apns_enabled: bool = False
    apns_team_id: str = ""
    apns_key_id: str = ""
    apns_auth_key: str = Field(default="", repr=False)
    apns_auth_key_b64: str = Field(default="", repr=False)
    apns_topic: str = "com.membri.reciapp"
    apns_environment: str = "production"
    trusted_proxy_ips: str = ""
    rate_limit_per_ip_per_minute: int = 90
    rate_limit_per_user_per_minute: int = 60
    rate_limit_extract_per_ip_per_minute: int = 7
    rate_limit_extract_per_user_per_minute: int = 5
    rate_limit_extract_daily_per_user: int = 50

    def validate_database(self) -> None:
        """Validate DATABASE_URL shape without logging credentials."""
        try:
            raw = self.database_url
            parsed = urlsplit(raw)
            if not raw or parsed.scheme != "postgresql" or not parsed.hostname:
                raise ValueError()
        except (ValueError, TypeError):
            raise RuntimeError("DATABASE_URL is required and must start with postgresql://") from None

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


settings = Settings()
