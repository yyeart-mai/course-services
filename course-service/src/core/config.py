"""Load and cache application settings from the environment."""

import functools
from typing import final

from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    """Define application metadata and debug settings."""

    title: str = "Course service title"
    description: str = "Course service description"
    summary: str = "Course service summary"
    version: str = "0.1.0"
    debug: bool = False


@final
class Settings(BaseSettings):
    """Load service configuration from environment variables and .env."""

    app: AppSettings
    database_url: str
    jwt_secret: SecretStr
    checker_url: AnyHttpUrl
    checker_timeout_seconds: float = Field(default=5.0, gt=0)
    checker_max_concurrency: int = Field(default=5, ge=1)
    worker_poll_interval_seconds: float = Field(default=1.0, gt=0)
    worker_lease_seconds: float = Field(default=30.0, gt=0)
    checker_max_attempts: int = Field(default=3, ge=1)

    @model_validator(mode="after")
    def check_worker_lease(self) -> Settings:
        """Ensure a worker lease exceeds the checker timeout."""
        margin_seconds = 5.0
        if self.worker_lease_seconds <= self.checker_timeout_seconds + margin_seconds:
            raise ValueError(
                "worker_lease_seconds must exceed checker_timeout_seconds "
                f"by at least {margin_seconds} seconds"
            )
        return self

    model_config = SettingsConfigDict(
        extra="allow",
        env_file=".env",
        env_nested_delimiter="__",
        env_file_encoding="utf-8",
    )


@functools.lru_cache
def get_settings() -> Settings:
    """Return the cached service settings."""
    return Settings()
