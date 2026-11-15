"""Settings for every service. One image, one settings class; values come from BR_* environment variables."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BR_", env_file=".env", extra="ignore")

    # --- runtime ---
    mode: str = "local"  # local: every service in one process; cluster: one service per deployment
    env: str = "dev"  # dev | test | prod
    service: str = "all"
    data_dir: str = ".data"
    public_url: str = "http://localhost:8000"
    web_dist: str = "web/dist"  # served by the local process; in the cluster the web image serves it

    # --- stores (cluster mode) ---
    database_url: str = ""  # e.g. postgresql+asyncpg://burnrate:***@pg-rw.data:5432/{service}
    kafka_bootstrap: str = ""  # e.g. burnrate-kafka-bootstrap.data:9092
    redis_url: str = ""  # e.g. redis://valkey.data:6379/0

    # --- security ---
    jwt_secret: str = "dev-secret-change-me-before-any-real-use-0000"
    internal_token: str = "dev-internal-token-change-me"
    cookie_secure: bool = False
    admin_token: str = "dev-admin-token"

    # --- product switches ---
    demo_enabled: bool = True
    default_timezone: str = "America/New_York"

    # --- language model: OpenAI by default, Claude selectable, fake for tests ---
    llm_provider: str = "openai"  # openai | anthropic | fake
    openai_api_key: str = ""
    openai_model: str = "gpt-5.4-mini"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5-5"
    llm_daily_budget_usd: float = 3.0  # hard ceiling for the whole deployment per UTC day
    llm_user_daily_budget_usd: float = 0.15  # per signed-in user per UTC day
    llm_guest_daily_budget_usd: float = 0.03  # per demo guest per UTC day

    # --- billing ---
    billing_provider: str = "demo"  # demo (test card on screen) | stripe
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""

    # --- notifications ---
    sms_provider: str = "inbox"  # inbox (captured, shown in the demo inbox) | twilio
    email_provider: str = "inbox"  # inbox | smtp
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from: str = ""
    smtp_url: str = ""
    mail_from: str = "Burn Rate <hello@burnrate.example>"

    @property
    def is_local(self) -> bool:
        return self.mode == "local"

    @property
    def is_test(self) -> bool:
        return self.env == "test"


@lru_cache
def settings() -> Settings:
    return Settings()


def reset_settings() -> None:
    """Tests change the environment between cases."""
    settings.cache_clear()
