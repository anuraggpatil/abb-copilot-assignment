"""Settings for the Alarm Management API simulator.

Separate from the copilot backend's settings even though both read the same `.env`: the
simulator stands in for a third-party source system, so it must not be able to see LLM
credentials or RAG configuration. Splitting the settings classes makes that boundary
mechanical rather than a matter of discipline.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AlarmApiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    host: str = Field(default="0.0.0.0", alias="ALARM_API_HOST")
    port: int = Field(default=8000, alias="ALARM_API_PORT")
    token: str = Field(default="demo-token", alias="ALARM_API_TOKEN")
    seed: int = Field(default=1729, alias="SEED_RANDOM_SEED")
    days_of_history: int = Field(default=400, alias="SEED_DAYS_OF_HISTORY")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # Cap on `page_size` so a caller (or a confused LLM) cannot ask for the whole dataset
    # in one response and blow up the copilot's context window.
    max_page_size: int = 500


@lru_cache(maxsize=1)
def get_settings() -> AlarmApiSettings:
    """Cached so every request shares one instance; cleared by tests via `cache_clear()`."""
    return AlarmApiSettings()
