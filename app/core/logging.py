"""
logging.py – Configure the standard logging setup for the application.

We use Python's built-in logging module so there are no extra dependencies.
The log level is read from Settings, and a consistent format is applied to
the root logger so all modules (including third-party ones) share the same
format.
"""

import logging

from app.config import Settings


def setup_logging(settings: Settings) -> None:
    """Configure root logger with the level from settings.

    Called once during app startup (lifespan).
    """
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    # Quiet down noisy third-party loggers that we don't need at DEBUG.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("multipart").setLevel(logging.WARNING)
