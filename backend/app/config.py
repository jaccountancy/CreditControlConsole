from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "production"
    port: int
    base_url: str
    database_url: str
    app_secret: str
    widget_token: str
    xero_client_id: str
    xero_client_secret: str
    xero_redirect_uri: str
    xero_scopes: str = "openid profile email offline_access accounting.invoices.read accounting.contacts.read"
    xero_state_ttl_seconds: int = 900
    device_code_ttl_minutes: int = 10
    session_ttl_days: int = 30
    statutory_interest_rate: float = 0.08

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("base_url", "database_url", "xero_redirect_uri")
    @classmethod
    def reject_local_values(cls, value: str) -> str:
        lowered = value.lower()
        if "localhost" in lowered or "127.0.0.1" in lowered:
            raise ValueError("Local connection values are disabled; use Railway-hosted URLs only.")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
