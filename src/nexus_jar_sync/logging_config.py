"""Dedicated rotating-file logging configuration for nexus-jar-sync."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from nexus_jar_sync.config import LoggingConfig


LOGGER_NAME = "nexus_jar_sync"


def configure_logging(config: LoggingConfig) -> logging.Logger:
    """Configure and return the package logger without changing the root logger."""
    config.file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, config.level))
    logger.propagate = False

    for handler in list(logger.handlers):
        if getattr(handler, "_nexus_jar_sync_managed", False):
            logger.removeHandler(handler)
            handler.close()

    handler = RotatingFileHandler(
        config.file,
        maxBytes=max(1, int(config.max_file_size_mb * 1024 * 1024)),
        backupCount=config.backup_count,
        encoding="utf-8",
    )
    handler._nexus_jar_sync_managed = True  # type: ignore[attr-defined]
    handler.setLevel(getattr(logging, config.level))
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    logger.addHandler(handler)
    return logger
