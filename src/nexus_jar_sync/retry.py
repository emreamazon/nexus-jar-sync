"""Bounded fixed-delay retry execution for transient project operations."""

from __future__ import annotations

from collections.abc import Callable
import logging
import time
from typing import TypeVar

from nexus_jar_sync.downloader import DownloadError
from nexus_jar_sync.nexus_client import NexusClientError


T = TypeVar("T")


class RetryExecutor:
    def __init__(
        self,
        *,
        sleep: Callable[[float], None] = time.sleep,
        logger: logging.Logger | None = None,
    ) -> None:
        self._sleep = sleep
        self._logger = logger or logging.getLogger("nexus_jar_sync.retry")

    def run(
        self,
        operation: Callable[[], T],
        *,
        retries: int,
        retry_delay_seconds: float,
        operation_name: str,
        target_id: str,
    ) -> T:
        total_attempts = 1 + retries
        for attempt in range(1, total_attempts + 1):
            try:
                return operation()
            except (NexusClientError, DownloadError) as error:
                if not error.retryable or attempt == total_attempts:
                    raise
                remaining = total_attempts - attempt
                self._logger.warning(
                    "Target %s: retrying %s (attempt %d/%d, %d remaining)",
                    target_id,
                    operation_name,
                    attempt + 1,
                    total_attempts,
                    remaining,
                )
                self._sleep(retry_delay_seconds)
        raise AssertionError("retry loop completed unexpectedly")
