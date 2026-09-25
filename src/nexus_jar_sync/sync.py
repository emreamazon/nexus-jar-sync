"""One-shot sequential synchronization service with per-target isolation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace as dataclass_replace
from datetime import datetime, timezone
from enum import Enum
import hmac
import logging
from pathlib import Path

from nexus_jar_sync.config import AppConfig, TargetConfig
from nexus_jar_sync.downloader import (
    ArtifactDownloader,
    DownloadDisposition,
    DownloadError,
    DownloadResult,
)
from nexus_jar_sync.nexus_client import NexusAsset, NexusClient, NexusClientError
from nexus_jar_sync.retry import RetryExecutor
from nexus_jar_sync.release import ReleaseAssembler
from nexus_jar_sync.release import METADATA_NAME
from nexus_jar_sync.state import ChangeDecision, StateError, StateStore, TargetState, determine_change


class TargetSyncStatus(Enum):
    CURRENT = "current"
    UPDATED = "updated"
    WOULD_UPDATE = "would_update"
    FAILED = "failed"


@dataclass(frozen=True)
class TargetSyncResult:
    target_id: str
    status: TargetSyncStatus
    version: str | None
    change: ChangeDecision | None
    message: str


@dataclass(frozen=True)
class SyncSummary:
    results: tuple[TargetSyncResult, ...]

    @property
    def updated_count(self) -> int:
        return sum(result.status is TargetSyncStatus.UPDATED for result in self.results)

    @property
    def current_count(self) -> int:
        return sum(result.status is TargetSyncStatus.CURRENT for result in self.results)

    @property
    def failed_count(self) -> int:
        return sum(result.status is TargetSyncStatus.FAILED for result in self.results)

    @property
    def would_update_count(self) -> int:
        return sum(result.status is TargetSyncStatus.WOULD_UPDATE for result in self.results)


class SyncService:
    def __init__(
        self,
        *,
        nexus_client: NexusClient,
        downloader: ArtifactDownloader,
        release_assembler: ReleaseAssembler | None = None,
        retry_executor: RetryExecutor,
        state_store_factory: Callable[[Path], StateStore] = StateStore,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        logger: logging.Logger | None = None,
    ) -> None:
        self._nexus_client = nexus_client
        self._downloader = downloader
        self._release_assembler = release_assembler
        self._retry = retry_executor
        self._state_store_factory = state_store_factory
        self._clock = clock
        self._logger = logger or logging.getLogger("nexus_jar_sync.sync")

    def run(self, config: AppConfig, *, dry_run: bool = False) -> SyncSummary:
        self._config = config
        enabled_targets = config.enabled_targets
        self._logger.info(
            "%s started with %d enabled targets",
            "Dry-run" if dry_run else "Run",
            len(enabled_targets),
        )
        store = self._state_store_factory(config.state.directory)
        results: list[TargetSyncResult] = []
        for target in enabled_targets:
            results.append(self._process_target(target, store, dry_run=dry_run))
        summary = SyncSummary(tuple(results))
        self._logger.info(
            "%s finished: %d updated, %d would update, %d current, %d failed",
            "Dry-run" if dry_run else "Run",
            summary.updated_count,
            summary.would_update_count,
            summary.current_count,
            summary.failed_count,
        )
        return summary

    def run_test_download(self, config: AppConfig, output_root: Path) -> SyncSummary:
        if self._release_assembler is None:
            raise ValueError("test download requires a release assembler")
        self._config = config
        results: list[TargetSyncResult] = []
        for target in config.enabled_targets:
            asset: NexusAsset | None = None
            try:
                asset = self._retry.run(
                    lambda: self._nexus_client.get_latest_asset(target),
                    retries=target.network.retries,
                    retry_delay_seconds=target.network.retry_delay_seconds,
                    operation_name="asset discovery",
                    target_id=target.id,
                )
                target_root = output_root / _safe_test_target_id(target.id)
                download = self._retry.run(
                    lambda: self._release_assembler.assemble(
                        asset, target, config.tools, destination_base=target_root,
                        secondary_resolver=lambda artifact_id: self._resolve_secondary(
                            target, asset.version, artifact_id
                        ),
                    ),
                    retries=target.network.retries,
                    retry_delay_seconds=target.network.retry_delay_seconds,
                    operation_name="test release download",
                    target_id=target.id,
                )
                self._validate_download_result_for_base(download, asset, target, target_root)
                results.append(TargetSyncResult(target.id, TargetSyncStatus.UPDATED, asset.version, None, f"Downloaded test release {asset.version} to {download.path.parent}"))
            except (NexusClientError, DownloadError) as error:
                results.append(TargetSyncResult(target.id, TargetSyncStatus.FAILED, asset.version if asset else None, None, str(error)))
        return SyncSummary(tuple(results))

    def close(self) -> None:
        """Close production-owned resources without closing injected sessions."""
        components = (self._nexus_client, self._release_assembler or self._downloader)
        for component in components:
            close = getattr(component, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    pass

    def _process_target(
        self, target: TargetConfig, store: StateStore, *, dry_run: bool
    ) -> TargetSyncResult:
        asset: NexusAsset | None = None
        change: ChangeDecision | None = None
        try:
            self._logger.info("Target %s: processing started", target.id)
            asset = self._retry.run(
                lambda: self._nexus_client.get_latest_asset(target),
                retries=target.network.retries,
                retry_delay_seconds=target.network.retry_delay_seconds,
                operation_name="asset discovery",
                target_id=target.id,
            )
            self._logger.info("Target %s: discovered version %s", target.id, asset.version)
            state = store.load(target.id)
            expected_path = ArtifactDownloader._validated_final_path(asset, target)
            change = determine_change(state, asset, expected_path)
            if dry_run and change is ChangeDecision.CURRENT:
                self._logger.info("Target %s: current; no change", target.id)
                return TargetSyncResult(
                    target_id=target.id,
                    status=TargetSyncStatus.CURRENT,
                    version=asset.version,
                    change=change,
                    message="Artifact is current",
                )

            if dry_run:
                self._logger.info("Target %s: change detected: %s", target.id, change.value)
                self._logger.info("Target %s: would update to version %s", target.id, asset.version)
                return TargetSyncResult(
                    target_id=target.id,
                    status=TargetSyncStatus.WOULD_UPDATE,
                    version=asset.version,
                    change=change,
                    message=f"Would update to version {asset.version}",
                )
            download = self._retry.run(
                lambda: self._release_assembler.assemble(
                    asset,
                    target,
                    self._config.tools,
                    secondary_resolver=lambda artifact_id: self._resolve_secondary(
                        target, asset.version, artifact_id
                    ),
                )
                if self._release_assembler is not None
                else self._downloader.download(asset, target),
                retries=target.network.retries,
                retry_delay_seconds=target.network.retry_delay_seconds,
                operation_name="artifact download",
                target_id=target.id,
            )
            self._validate_download_result(download, asset, target)
            if download.disposition is DownloadDisposition.REUSED:
                self._logger.info("Target %s: verified existing artifact", target.id)
            else:
                self._logger.info(
                    "Target %s: downloaded %d bytes", target.id, download.bytes_written
                )

            if change is ChangeDecision.CURRENT and download.disposition is DownloadDisposition.REUSED:
                self._logger.info("Target %s: current; no change", target.id)
                return TargetSyncResult(
                    target_id=target.id,
                    status=TargetSyncStatus.CURRENT,
                    version=asset.version,
                    change=change,
                    message="Artifact is current",
                )

            timestamp = self._clock()
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError("sync clock must return a timezone-aware datetime")
            store.save(
                target.id,
                TargetState(
                    version=asset.version,
                    path=str(download.path),
                    checksum_algorithm=download.checksum_algorithm,
                    checksum=download.checksum,
                    downloaded_at=timestamp.isoformat(),
                    release_directory=str(download.path.parent),
                    metadata_path=str(download.path.parent / METADATA_NAME),
                ),
            )
            self._logger.info("Target %s: state updated", target.id)
            return TargetSyncResult(
                target_id=target.id,
                status=TargetSyncStatus.UPDATED,
                version=asset.version,
                change=change,
                message=f"Updated to version {asset.version}",
            )
        except (NexusClientError, DownloadError, StateError) as error:
            self._logger.error("Target %s failed: %s", target.id, error)
            return TargetSyncResult(
                target_id=target.id,
                status=TargetSyncStatus.FAILED,
                version=asset.version if asset is not None else None,
                change=change,
                message=str(error),
            )

    def _resolve_secondary(
        self, target: TargetConfig, version: str, artifact_id: str
    ) -> NexusAsset:
        return self._retry.run(
            lambda: self._nexus_client.get_asset_at_version(target, artifact_id, version),
            retries=target.network.retries,
            retry_delay_seconds=target.network.retry_delay_seconds,
            operation_name="secondary artifact discovery",
            target_id=target.id,
        )

    @staticmethod
    def _validate_download_result(
        result: DownloadResult, asset: NexusAsset, target: TargetConfig
    ) -> None:
        expected_path = ArtifactDownloader._validated_final_path(asset, target)
        expected_checksum = asset.checksums.get(result.checksum_algorithm)
        if (
            result.filename != asset.filename
            or result.path.resolve(strict=False) != expected_path
            or expected_checksum is None
            or not hmac.compare_digest(result.checksum.lower(), expected_checksum.lower())
        ):
            raise DownloadError(
                f"Download result does not match selected asset for target '{target.id}'"
            )

    @staticmethod
    def _validate_download_result_for_base(
        result: DownloadResult, asset: NexusAsset, target: TargetConfig, base: Path
    ) -> None:
        temporary_target = dataclass_replace(
            target, destination=dataclass_replace(target.destination, directory=base)
        )
        SyncService._validate_download_result(result, asset, temporary_target)


def _safe_test_target_id(value: str) -> str:
    if not value or value in {".", ".."} or any(character in value for character in '/\\<>:"|?*'):
        raise DownloadError("Unsafe target id for test output")
    return value
