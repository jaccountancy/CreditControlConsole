from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    port: int = 8000
    base_url: str = "http://127.0.0.1:8000"
    database_url: str = "postgresql://postgres:postgres@localhost:5432/credit_control"
    app_secret: str = "replace-with-a-long-random-secret"
    widget_token: str = "replace-with-a-long-random-widget-token"
    xero_client_id: str = ""
    xero_client_secret: str = ""
    xero_redirect_uri: str = "http://127.0.0.1:8000/auth/xero/callback"
    xero_scopes: str = "openid profile email offline_access accounting.transactions.read accounting.contacts.read"
    xero_state_ttl_seconds: int = 900
    device_code_ttl_minutes: int = 10
    session_ttl_days: int = 30
    statutory_interest_rate: float = 0.08

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
