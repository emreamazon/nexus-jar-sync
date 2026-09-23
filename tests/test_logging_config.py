from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from nexus_jar_sync.config import LoggingConfig
from nexus_jar_sync.logging_config import LOGGER_NAME, configure_logging


def close_managed_handlers() -> None:
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        if getattr(handler, "_nexus_jar_sync_managed", False):
            logger.removeHandler(handler)
            handler.close()


def test_rotating_utf8_handler_configuration_and_no_duplicates(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "sync.log"
    config = LoggingConfig(
        level="WARNING",
        file=log_file,
        max_file_size_mb=0.25,
        backup_count=4,
    )
    assert not log_file.parent.exists()
    try:
        logger = configure_logging(config)
        assert log_file.parent.is_dir()
        handler = next(
            item for item in logger.handlers if getattr(item, "_nexus_jar_sync_managed", False)
        )
        assert isinstance(handler, RotatingFileHandler)
        assert handler.encoding.lower().replace("-", "") == "utf8"
        assert handler.maxBytes == int(0.25 * 1024 * 1024)
        assert handler.backupCount == 4
        assert logger.level == logging.WARNING

        configure_logging(config)
        assert sum(
            getattr(item, "_nexus_jar_sync_managed", False) for item in logger.handlers
        ) == 1
    finally:
        close_managed_handlers()


def test_rotation_and_operational_message_are_utf8(tmp_path: Path) -> None:
    log_file = tmp_path / "sync.log"
    config = LoggingConfig(
        level="INFO",
        file=log_file,
        max_file_size_mb=0.00005,
        backup_count=1,
    )
    try:
        logger = configure_logging(config)
        logger.info("Target project-α: processing started")
        logger.info("x" * 200)
        for handler in logger.handlers:
            handler.flush()
        combined = "".join(
            path.read_text(encoding="utf-8") for path in (log_file, Path(f"{log_file}.1")) if path.exists()
        )
        assert "project-α" in combined
        assert Path(f"{log_file}.1").exists()
    finally:
        close_managed_handlers()


def test_root_handlers_unchanged_and_credentials_not_logged(tmp_path: Path) -> None:
    root = logging.getLogger()
    unrelated = logging.NullHandler()
    root.addHandler(unrelated)
    username = "private-user"
    password = "private-password"
    log_file = tmp_path / "sync.log"
    try:
        logger = configure_logging(LoggingConfig(file=log_file))
        logger.error("Target example failed safely")
        for handler in logger.handlers:
            handler.flush()
        text = log_file.read_text(encoding="utf-8")
        assert username not in text
        assert password not in text
        assert unrelated in root.handlers
    finally:
        close_managed_handlers()
        root.removeHandler(unrelated)
