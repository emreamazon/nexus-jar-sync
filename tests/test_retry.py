from __future__ import annotations

import logging

import pytest

from nexus_jar_sync.downloader import DownloadError
from nexus_jar_sync.nexus_client import NexusClientError
from nexus_jar_sync.retry import RetryExecutor


class ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def test_success_first_attempt_has_no_sleep() -> None:
    sleeps: list[float] = []
    assert RetryExecutor(sleep=sleeps.append).run(
        lambda: "ok",
        retries=3,
        retry_delay_seconds=2,
        operation_name="discovery",
        target_id="one",
    ) == "ok"
    assert sleeps == []


def test_retryable_failures_use_bounded_attempts_and_fixed_delays() -> None:
    sleeps: list[float] = []
    calls = 0

    def operation() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise NexusClientError("sanitized transient failure", retryable=True)
        return "ok"

    result = RetryExecutor(sleep=sleeps.append).run(
        operation,
        retries=3,
        retry_delay_seconds=1.5,
        operation_name="asset discovery",
        target_id="one",
    )
    assert result == "ok"
    assert calls == 3
    assert sleeps == [1.5, 1.5]


def test_exhausted_retries_raise_final_sanitized_exception() -> None:
    sleeps: list[float] = []
    errors: list[DownloadError] = []

    def operation() -> None:
        error = DownloadError(f"safe failure {len(errors) + 1}", retryable=True)
        errors.append(error)
        raise error

    with pytest.raises(DownloadError) as caught:
        RetryExecutor(sleep=sleeps.append).run(
            operation,
            retries=2,
            retry_delay_seconds=4,
            operation_name="download",
            target_id="one",
        )
    assert caught.value is errors[-1]
    assert len(errors) == 3
    assert sleeps == [4, 4]


def test_retries_zero_and_non_retryable_failure_make_one_attempt() -> None:
    for error, retries in (
        (NexusClientError("not retryable"), 3),
        (NexusClientError("retryable but no retries", retryable=True), 0),
    ):
        calls = 0
        sleeps: list[float] = []

        def operation() -> None:
            nonlocal calls
            calls += 1
            raise error

        with pytest.raises(NexusClientError) as caught:
            RetryExecutor(sleep=sleeps.append).run(
                operation,
                retries=retries,
                retry_delay_seconds=1,
                operation_name="discovery",
                target_id="one",
            )
        assert caught.value is error
        assert calls == 1
        assert sleeps == []


def test_retry_log_has_target_attempts_and_no_credentials() -> None:
    logger = logging.Logger("retry-test")
    handler = ListHandler()
    logger.addHandler(handler)
    calls = 0
    username = "private-user"
    password = "private-password"

    def operation() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise NexusClientError(f"{username}:{password}", retryable=True)
        return "ok"

    assert RetryExecutor(sleep=lambda _: None, logger=logger).run(
        operation,
        retries=1,
        retry_delay_seconds=0,
        operation_name="asset discovery",
        target_id="target-one",
    ) == "ok"
    text = " ".join(handler.messages)
    assert "target-one" in text
    assert "attempt 2/2" in text
    assert username not in text
    assert password not in text
