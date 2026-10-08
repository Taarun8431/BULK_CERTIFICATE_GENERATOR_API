"""
config.py – Application settings loaded from environment variables.

Using a plain frozen dataclass + lru_cache so settings are read once,
immutable throughout the process lifetime, and easy to override in tests.
"""

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    """All configuration knobs, with safe defaults for local development."""

    # Database – swap to postgres:// to use PostgreSQL without changing code.
    database_url: str = "sqlite:///./certificates.db"

    # Where generated PDF files are stored on disk.
    storage_dir: str = "./storage"

    # Hard ceiling: requests with more recipients than this get 422.
    max_recipients_per_request: int = 1000

    # Individual name length ceiling.
    max_name_length: int = 100

    log_level: str = "INFO"


def _build_settings() -> Settings:
    """Read settings from environment variables, falling back to defaults."""
    return Settings(
        database_url=os.getenv("DATABASE_URL", "sqlite:///./certificates.db"),
        storage_dir=os.getenv("STORAGE_DIR", "./storage"),
        max_recipients_per_request=int(
            os.getenv("MAX_RECIPIENTS_PER_REQUEST", "1000")
        ),
        max_name_length=int(os.getenv("MAX_NAME_LENGTH", "100")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton – same object for the lifetime of the process.

    Tests override this dependency to inject custom settings
    (e.g. a temp STORAGE_DIR and an in-memory SQLite URL).
    """
    return _build_settings()
