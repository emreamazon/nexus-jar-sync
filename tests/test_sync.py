from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
from typing import Any

import pytest

from nexus_jar_sync.config import (
    AppConfig,
    ArtifactConfig,
    AuthConfig,
    DestinationConfig,
    NetworkConfig,
    NexusConfig,
    StateConfig,
    TargetConfig,
)
from nexus_jar_sync.downloader import DownloadDisposition, DownloadError, DownloadResult
from nexus_jar_sync.nexus_client import NexusAsset, NexusClientError
from nexus_jar_sync.retry import RetryExecutor
from nexus_jar_sync.state import ChangeDecision, StateError, TargetState
from nexus_jar_sync.sync import SyncService, TargetSyncStatus


SHA256 = "a" * 64
NOW = datetime(2026, 9, 23, 15, 30, tzinfo=timezone(timedelta(hours=3)))


def make_target(target_id: str, destination: Path, *, enabled: bool = True, retries: int = 0) -> TargetConfig:
    return TargetConfig(
        id=target_id,
        enabled=enabled,
        nexus=NexusConfig("https://nexus.example.com", "releases", "com.example", "application"),
        destination=DestinationConfig(destination),
        network=NetworkConfig(retries=retries, retry_delay_seconds=2),
        auth=AuthConfig(),
        artifact=ArtifactConfig(),
    )


def make_asset(version: str = "2.0", *, path: str | None = None, checksum: str = SHA256) -> NexusAsset:
    filename = f"application-{version}.jar"
    return NexusAsset(
        version=version,
        filename=filename,
        download_url="https://downloads.example.com/application.jar",
        path=path or f"com/example/application/{version}/{filename}",
        checksums={"sha256": checksum},
    )


def make_state(
    version: str = "2.0", *, path: str | None = None, checksum: str = SHA256
) -> TargetState:
    return TargetState(
        version=version,
        path=path or f"com/example/application/{version}/application-{version}.jar",
        checksum_algorithm="sha256",
        checksum=checksum,
        downloaded_at="2026-01-01T00:00:00+00:00",
    )


class FakeClient:
    def __init__(self, outcomes: dict[str, list[Any]]) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []

    def get_latest_asset(self, target: TargetConfig) -> NexusAsset:
        self.calls.append(target.id)
        outcome = self.outcomes[target.id].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeDownloader:
    def __init__(self, outcomes: dict[str, list[Any]] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.calls: list[str] = []

    def download(self, asset: NexusAsset, target: TargetConfig) -> DownloadResult:
        self.calls.append(target.id)
        if target.id in self.outcomes:
            outcome = self.outcomes[target.id].pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return DownloadResult(
            path=(target.destination.directory / asset.version / asset.filename).resolve(strict=False),
            filename=asset.filename,
            bytes_written=100,
            checksum_algorithm="sha256",
            checksum=asset.checksums["sha256"],
            disposition=DownloadDisposition.REUSED,
        )


class FakeLifecycle:
    def __init__(self, failures: set[str] | None = None, before_call: Any = None) -> None:
        self.failures = failures or set()
        self.before_call = before_call
        self.calls: list[str] = []

    def apply_retention(self, target: TargetConfig, current_path: Path) -> tuple[Path, ...]:
        self.calls.append(target.id)
        if self.before_call is not None:
            self.before_call()
        if target.id in self.failures:
            raise RuntimeError(f"retired lifecycle invoked for target '{target.id}'")
        return ()


class FakeStore:
    def __init__(
        self,
        states: dict[str, TargetState | None] | None = None,
        *,
        load_failures: set[str] | None = None,
        save_failures: set[str] | None = None,
    ) -> None:
        self.states = states or {}
        self.load_failures = load_failures or set()
        self.save_failures = save_failures or set()
        self.loads: list[str] = []
        self.saves: list[tuple[str, TargetState]] = []

    def load(self, target_id: str) -> TargetState | None:
        self.loads.append(target_id)
        if target_id in self.load_failures:
            raise StateError(f"state load failed for target '{target_id}'")
        return self.states.get(target_id)

    def save(self, target_id: str, state: TargetState) -> None:
        if target_id in self.save_failures:
            raise StateError(f"state save failed for target '{target_id}'")
        self.saves.append((target_id, state))
        self.states[target_id] = state


def make_service(
    client: FakeClient,
    downloader: FakeDownloader,
    lifecycle: FakeLifecycle,
    store: FakeStore,
    *,
    sleeps: list[float] | None = None,
    logger: logging.Logger | None = None,
) -> SyncService:
    sleep_calls = sleeps if sleeps is not None else []
    return SyncService(
        nexus_client=client,  # type: ignore[arg-type]
        downloader=downloader,  # type: ignore[arg-type]
        retry_executor=RetryExecutor(sleep=sleep_calls.append, logger=logger),
        state_store_factory=lambda path: store,  # type: ignore[return-value]
        clock=lambda: NOW,
        logger=logger,
    )


def app_config(tmp_path: Path, *targets: TargetConfig) -> AppConfig:
    return AppConfig(targets=tuple(targets), state=StateConfig(tmp_path / "state"))


def test_disabled_targets_skipped_and_enabled_order_preserved(tmp_path: Path) -> None:
    targets = (
        make_target("one", tmp_path / "one"),
        make_target("disabled", tmp_path / "disabled", enabled=False),
        make_target("two", tmp_path / "two"),
    )
    client = FakeClient({"one": [make_asset()], "two": [make_asset()]})
    store = FakeStore({
        "one": make_state(path=str((tmp_path / "one" / "2.0" / "application-2.0.jar").resolve())),
        "two": make_state(path=str((tmp_path / "two" / "2.0" / "application-2.0.jar").resolve())),
    })
    summary = make_service(client, FakeDownloader(), FakeLifecycle(), store).run(
        app_config(tmp_path, *targets)
    )
    assert [result.target_id for result in summary.results] == ["one", "two"]
    assert client.calls == ["one", "two"]
    assert summary.current_count == 2
    assert summary.updated_count == 0
    assert summary.failed_count == 0


def test_zero_enabled_targets_returns_empty_summary(tmp_path: Path) -> None:
    target = make_target("disabled", tmp_path, enabled=False)
    summary = make_service(FakeClient({}), FakeDownloader(), FakeLifecycle(), FakeStore()).run(
        app_config(tmp_path, target)
    )
    assert summary.results == ()
    assert summary.updated_count == summary.current_count == summary.failed_count == 0


@pytest.mark.parametrize(
    ("state", "expected_change"),
    [
        (None, ChangeDecision.FIRST_RUN),
        (make_state(version="1.0"), ChangeDecision.VERSION_CHANGED),
        (make_state(checksum="b" * 64), ChangeDecision.CHECKSUM_CHANGED),
        (make_state(path="other/path.jar"), ChangeDecision.PATH_CHANGED),
    ],
)
def test_update_changes_download_retain_and_save_verified_state(
    tmp_path: Path, state: TargetState | None, expected_change: ChangeDecision
) -> None:
    target = make_target("one", tmp_path / "destination")
    asset = make_asset()
    if state is not None and expected_change is not ChangeDecision.PATH_CHANGED:
        state = replace(
            state,
            path=str((target.destination.directory / state.version / f"application-{state.version}.jar").resolve()),
        )
    store = FakeStore({"one": state})
    downloader = FakeDownloader()
    lifecycle = FakeLifecycle(before_call=lambda: assert_no_saves(store))
    summary = make_service(FakeClient({"one": [asset]}), downloader, lifecycle, store).run(
        app_config(tmp_path, target)
    )
    result = summary.results[0]
    assert result.status is TargetSyncStatus.UPDATED
    assert result.change is expected_change
    assert downloader.calls == ["one"]
    assert lifecycle.calls == []
    saved = store.saves[0][1]
    assert saved.version == asset.version
    assert saved.path == str((target.destination.directory / asset.version / asset.filename).resolve())
    assert saved.checksum_algorithm == "sha256"
    assert saved.checksum == SHA256
    assert saved.downloaded_at == NOW.isoformat()
    assert datetime.fromisoformat(saved.downloaded_at).utcoffset() is not None


def assert_no_saves(store: FakeStore) -> None:
    assert store.saves == []


def test_current_target_has_no_mutation(tmp_path: Path) -> None:
    target = make_target("one", tmp_path)
    downloader = FakeDownloader()
    lifecycle = FakeLifecycle()
    deployed = tmp_path / "2.0" / "application-2.0.jar"
    deployed.parent.mkdir()
    deployed.write_bytes(b"placeholder")
    # The fake downloader models local verification; state uses the versioned path.
    store = FakeStore({"one": make_state(path=str(deployed.resolve()))})
    downloader.outcomes["one"] = [DownloadResult(
        path=deployed.resolve(), filename="application-2.0.jar", bytes_written=11,
        checksum_algorithm="sha256", checksum=SHA256,
        disposition=DownloadDisposition.REUSED,
    )]
    result = make_service(FakeClient({"one": [make_asset()]}), downloader, lifecycle, store).run(
        app_config(tmp_path, target)
    ).results[0]
    assert result.status is TargetSyncStatus.CURRENT
    assert downloader.calls == ["one"]
    assert lifecycle.calls == []
    assert store.saves == []


def test_discovery_and_download_retries_continue_pipeline(tmp_path: Path) -> None:
    target = make_target("one", tmp_path, retries=1)
    asset = make_asset()
    client = FakeClient(
        {"one": [NexusClientError("transient discovery", retryable=True), asset]}
    )
    successful_result = DownloadResult(
        path=(tmp_path / asset.version / asset.filename).resolve(strict=False),
        filename=asset.filename,
        bytes_written=10,
        checksum_algorithm="sha256",
        checksum=SHA256,
    )
    downloader = FakeDownloader(
        {"one": [DownloadError("transient download", retryable=True), successful_result]}
    )
    sleeps: list[float] = []
    store = FakeStore()
    result = make_service(
        client, downloader, FakeLifecycle(), store, sleeps=sleeps
    ).run(app_config(tmp_path, target)).results[0]
    assert result.status is TargetSyncStatus.UPDATED
    assert client.calls == ["one", "one"]
    assert downloader.calls == ["one", "one"]
    assert sleeps == [2, 2]
    assert len(store.saves) == 1


@pytest.mark.parametrize("stage", ["discovery", "state-load", "download", "state-save"])
def test_operational_failures_stop_downstream_and_return_failed(
    tmp_path: Path, stage: str
) -> None:
    target = make_target("one", tmp_path, retries=1)
    asset = make_asset()
    client_outcomes: list[Any] = [asset]
    downloader_outcomes: dict[str, list[Any]] | None = None
    store = FakeStore()
    lifecycle = FakeLifecycle()
    if stage == "discovery":
        client_outcomes = [
            NexusClientError("safe discovery failure", retryable=True),
            NexusClientError("safe discovery failure", retryable=True),
        ]
    elif stage == "state-load":
        store.load_failures.add("one")
    elif stage == "download":
        downloader_outcomes = {
            "one": [
                DownloadError("safe download failure", retryable=True),
                DownloadError("safe download failure", retryable=True),
            ]
        }
    else:
        store.save_failures.add("one")
    downloader = FakeDownloader(downloader_outcomes)
    result = make_service(
        FakeClient({"one": client_outcomes}), downloader, lifecycle, store
    ).run(app_config(tmp_path, target)).results[0]
    assert result.status is TargetSyncStatus.FAILED
    assert "safe" in result.message or "failed" in result.message
    if stage == "discovery":
        assert result.version is None
        assert result.change is None
    elif stage == "state-load":
        assert result.version == asset.version
        assert result.change is None
    else:
        assert result.version == asset.version
        assert result.change is ChangeDecision.FIRST_RUN
    assert store.saves == []
    if stage == "discovery":
        assert len(client_outcomes) == 0
        assert store.loads == [] and downloader.calls == [] and lifecycle.calls == []
    elif stage == "state-load":
        assert downloader.calls == [] and lifecycle.calls == []
    elif stage == "download":
        assert downloader.calls == ["one", "one"]
        assert lifecycle.calls == []


def test_failed_target_does_not_stop_later_target_and_counts_are_correct(tmp_path: Path) -> None:
    targets = (make_target("bad", tmp_path / "bad"), make_target("good", tmp_path / "good"))
    client = FakeClient(
        {"bad": [NexusClientError("safe failure")], "good": [make_asset()]}
    )
    store = FakeStore({"good": make_state(path=str((tmp_path / "good" / "2.0" / "application-2.0.jar").resolve()))})
    summary = make_service(client, FakeDownloader(), FakeLifecycle(), store).run(
        app_config(tmp_path, *targets)
    )
    assert [result.status for result in summary.results] == [
        TargetSyncStatus.FAILED,
        TargetSyncStatus.CURRENT,
    ]
    assert client.calls == ["bad", "good"]
    assert summary.failed_count == 1
    assert summary.current_count == 1


def test_mismatched_download_result_stops_before_state(tmp_path: Path) -> None:
    target = make_target("one", tmp_path)
    asset = make_asset()
    mismatched = DownloadResult(
        path=(tmp_path / "other.jar").resolve(strict=False),
        filename="other.jar",
        bytes_written=1,
        checksum_algorithm="sha256",
        checksum=SHA256,
    )
    lifecycle = FakeLifecycle()
    store = FakeStore()
    result = make_service(
        FakeClient({"one": [asset]}),
        FakeDownloader({"one": [mismatched]}),
        lifecycle,
        store,
    ).run(app_config(tmp_path, target)).results[0]
    assert result.status is TargetSyncStatus.FAILED
    assert lifecycle.calls == []
    assert store.saves == []


def test_state_save_failure_does_not_remove_deployed_artifact(tmp_path: Path) -> None:
    target = make_target("one", tmp_path)
    asset = make_asset()
    deployed = tmp_path / asset.version / asset.filename
    deployed.parent.mkdir()
    deployed.write_bytes(b"verified artifact")
    result_value = DownloadResult(
        path=deployed,
        filename=asset.filename,
        bytes_written=len(b"verified artifact"),
        checksum_algorithm="sha256",
        checksum=SHA256,
    )
    store = FakeStore(save_failures={"one"})
    result = make_service(
        FakeClient({"one": [asset]}),
        FakeDownloader({"one": [result_value]}),
        FakeLifecycle(),
        store,
    ).run(app_config(tmp_path, target)).results[0]
    assert result.status is TargetSyncStatus.FAILED
    assert deployed.read_bytes() == b"verified artifact"


def test_unexpected_programming_error_is_not_swallowed(tmp_path: Path) -> None:
    target = make_target("one", tmp_path)
    with pytest.raises(RuntimeError, match="programming defect"):
        make_service(
            FakeClient({"one": [RuntimeError("programming defect")]}),
            FakeDownloader(),
            FakeLifecycle(),
            FakeStore(),
        ).run(app_config(tmp_path, target))


def test_downloader_programming_error_is_not_retried_or_swallowed(tmp_path: Path) -> None:
    target = make_target("one", tmp_path, retries=3)
    downloader = FakeDownloader({"one": [RuntimeError("stream programming defect")]})
    sleeps: list[float] = []
    store = FakeStore()
    lifecycle = FakeLifecycle()
    with pytest.raises(RuntimeError, match="stream programming defect"):
        make_service(
            FakeClient({"one": [make_asset()]}),
            downloader,
            lifecycle,
            store,
            sleeps=sleeps,
        ).run(app_config(tmp_path, target))
    assert downloader.calls == ["one"]
    assert sleeps == []
    assert lifecycle.calls == []
    assert store.saves == []


def test_credentials_do_not_appear_in_failed_result_or_logs(tmp_path: Path) -> None:
    username = "private-user"
    password = "private-password"
    logger = logging.Logger("sync-test")
    messages: list[str] = []

    class Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    logger.addHandler(Handler())
    target = make_target("one", tmp_path)
    result = make_service(
        FakeClient({"one": [NexusClientError("sanitized failure")]}),
        FakeDownloader(),
        FakeLifecycle(),
        FakeStore(),
        logger=logger,
    ).run(app_config(tmp_path, target)).results[0]
    combined = result.message + " ".join(messages)
    assert "Target one" in combined
    assert "Run finished" in combined
    assert username not in combined
    assert password not in combined


@pytest.mark.parametrize(
    ("state", "decision"),
    [
        (None, ChangeDecision.FIRST_RUN),
        (make_state("1.0"), ChangeDecision.VERSION_CHANGED),
        (make_state(checksum="b" * 64), ChangeDecision.CHECKSUM_CHANGED),
        (make_state(path="other/path.jar"), ChangeDecision.PATH_CHANGED),
    ],
)
def test_dry_run_reports_changes_without_mutation(
    tmp_path: Path, state: TargetState | None, decision: ChangeDecision
) -> None:
    target = make_target("one", tmp_path / "destination")
    if state is not None and decision is not ChangeDecision.PATH_CHANGED:
        state = replace(
            state,
            path=str((target.destination.directory / state.version / f"application-{state.version}.jar").resolve()),
        )
    store = FakeStore({"one": state})
    downloader = FakeDownloader()
    lifecycle = FakeLifecycle()

    summary = make_service(
        FakeClient({"one": [make_asset()]}), downloader, lifecycle, store
    ).run(app_config(tmp_path, target), dry_run=True)

    assert summary.results[0].status is TargetSyncStatus.WOULD_UPDATE
    assert summary.results[0].change is decision
    assert summary.would_update_count == 1
    assert downloader.calls == []
    assert lifecycle.calls == []
    assert store.saves == []
    assert not target.destination.directory.exists()
    assert not (tmp_path / "state").exists()


def test_dry_run_current_target_remains_current_without_mutation(tmp_path: Path) -> None:
    target = make_target("one", tmp_path / "destination")
    store = FakeStore({"one": make_state(path=str((target.destination.directory / "2.0" / "application-2.0.jar").resolve()))})
    downloader = FakeDownloader()
    lifecycle = FakeLifecycle()
    summary = make_service(
        FakeClient({"one": [make_asset()]}), downloader, lifecycle, store
    ).run(app_config(tmp_path, target), dry_run=True)
    assert summary.results[0].status is TargetSyncStatus.CURRENT
    assert downloader.calls == lifecycle.calls == []
    assert store.saves == []


def test_dry_run_preserves_order_retries_and_failure_isolation(tmp_path: Path) -> None:
    targets = tuple(make_target(name, tmp_path / name, retries=1) for name in ("one", "two", "three"))
    client = FakeClient(
        {
            "one": [NexusClientError("temporary", retryable=True), make_asset()],
            "two": [NexusClientError("permanent")],
            "three": [make_asset()],
        }
    )
    store = FakeStore({"three": make_state(path=str((tmp_path / "three" / "2.0" / "application-2.0.jar").resolve()))})
    sleeps: list[float] = []
    summary = make_service(
        client, FakeDownloader(), FakeLifecycle(), store, sleeps=sleeps
    ).run(app_config(tmp_path, *targets), dry_run=True)
    assert [result.target_id for result in summary.results] == ["one", "two", "three"]
    assert [result.status for result in summary.results] == [
        TargetSyncStatus.WOULD_UPDATE,
        TargetSyncStatus.FAILED,
        TargetSyncStatus.CURRENT,
    ]
    assert sleeps == [2]


def test_dry_run_isolates_expected_state_error_but_propagates_programming_error(
    tmp_path: Path,
) -> None:
    target = make_target("one", tmp_path / "one")
    expected = make_service(
        FakeClient({"one": [make_asset()]}), FakeDownloader(), FakeLifecycle(),
        FakeStore(load_failures={"one"}),
    ).run(app_config(tmp_path, target), dry_run=True)
    assert expected.results[0].status is TargetSyncStatus.FAILED
    assert expected.results[0].version == "2.0"
    assert expected.results[0].change is None

    class BrokenStore(FakeStore):
        def load(self, target_id: str) -> TargetState | None:
            raise RuntimeError("programming defect")

    with pytest.raises(RuntimeError, match="programming defect"):
        make_service(
            FakeClient({"one": [make_asset()]}), FakeDownloader(), FakeLifecycle(), BrokenStore()
        ).run(app_config(tmp_path, target), dry_run=True)
