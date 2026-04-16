from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Reads from environment variables (Docker Compose injects from .env via ${VAR}).
    Falls back to .env in CWD for local dev outside Docker.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,
    )

    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    database_path: str = "/data/ledger.db"
    poc_api_key: str | None = None
    temporal_address: str = ""


def get_settings() -> Settings:
    return Settings()
