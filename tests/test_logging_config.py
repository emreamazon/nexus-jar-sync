from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from nexus_jar_sync.config import LoggingConfig
from nexus_jar_sync.logging_config import (
    BoundedRotatingFileHandler,
    LOGGER_NAME,
    configure_logging,
)


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


def test_zero_backup_rollover_bounds_active_file_without_archive(tmp_path: Path) -> None:
    log_file = tmp_path / "sync.log"
    threshold_bytes = 80
    record_text = "x" * 50
    config = LoggingConfig(
        file=log_file,
        max_file_size_mb=threshold_bytes / (1024 * 1024),
        backup_count=0,
    )
    try:
        logger = configure_logging(config)
        for index in range(8):
            logger.info("record-%d %s", index, record_text)
        for handler in logger.handlers:
            handler.flush()
        assert log_file.exists()
        assert not Path(f"{log_file}.1").exists()
        assert 0 < log_file.stat().st_size <= threshold_bytes + len(record_text) + 100
        assert "record-7" in log_file.read_text(encoding="utf-8")
    finally:
        close_managed_handlers()


def test_positive_backup_rotation_still_creates_archive(tmp_path: Path) -> None:
    log_file = tmp_path / "sync.log"
    config = LoggingConfig(
        file=log_file,
        max_file_size_mb=80 / (1024 * 1024),
        backup_count=1,
    )
    try:
        logger = configure_logging(config)
        for index in range(4):
            logger.info("record-%d %s", index, "x" * 50)
        for handler in logger.handlers:
            handler.flush()
        assert Path(f"{log_file}.1").exists()
    finally:
        close_managed_handlers()


def test_reconfiguration_closes_and_replaces_managed_handler(tmp_path: Path) -> None:
    config = LoggingConfig(file=tmp_path / "sync.log")
    try:
        logger = configure_logging(config)
        original = next(
            handler
            for handler in logger.handlers
            if isinstance(handler, BoundedRotatingFileHandler)
        )
        replacement_logger = configure_logging(config)
        replacement = next(
            handler
            for handler in replacement_logger.handlers
            if isinstance(handler, BoundedRotatingFileHandler)
        )
        assert replacement is not original
        assert original.stream is None
        replacement_logger.info("logging continues after reconfiguration")
        replacement.flush()
        assert "logging continues" in config.file.read_text(encoding="utf-8")
    finally:
        close_managed_handlers()
