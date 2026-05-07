from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    port: int = 8000
    database_url: str = "postgresql://postgres:postgres@localhost:5432/credit_control"
    api_token: str = "replace-with-a-long-random-string"
    xero_client_id: str = ""
    xero_client_secret: str = ""
    xero_scopes: str = "accounting.transactions.read accounting.contacts.read"
    dashboard_stale_after_minutes: int = 30

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
