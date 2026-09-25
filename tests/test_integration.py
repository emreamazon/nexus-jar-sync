from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator

import requests

from nexus_jar_sync.config import (
    AppConfig, ArtifactConfig, AuthConfig, DestinationConfig, NetworkConfig,
    NexusConfig, StateConfig, TargetConfig,
)
from nexus_jar_sync.downloader import ArtifactDownloader
from nexus_jar_sync.nexus_client import NexusClient
from nexus_jar_sync.retry import RetryExecutor
from nexus_jar_sync.state import StateStore, TargetState
from nexus_jar_sync.sync import SyncService, TargetSyncStatus


class SearchResponse:
    status_code = 200
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
    def json(self) -> dict[str, Any]:
        return self.payload


class SearchSession:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls = 0
    def get(self, url: str, **kwargs: Any) -> SearchResponse:
        self.calls += 1
        return SearchResponse(self.payload)


class SequentialSearchSession:
    def __init__(self, results: list[SearchResponse | Exception]) -> None:
        self.results = results
    def get(self, url: str, **kwargs: Any) -> SearchResponse:
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class DownloadResponse:
    status_code = 200
    headers: dict[str, str]
    def __init__(self, content: bytes, error: Exception | None = None) -> None:
        self.content_bytes = content
        self.error = error
        self.headers = {"Content-Length": str(len(content))}
    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        yield self.content_bytes[: max(1, len(self.content_bytes) // 2)]
        if self.error:
            raise self.error
        yield self.content_bytes[max(1, len(self.content_bytes) // 2) :]
    def close(self) -> None:
        pass


class DownloadSession:
    def __init__(self, response: DownloadResponse) -> None:
        self.response = response
        self.calls = 0
    def get(self, url: str, **kwargs: Any) -> DownloadResponse:
        self.calls += 1
        return self.response


def target(tmp_path: Path) -> TargetConfig:
    return TargetConfig(
        id="application", enabled=True,
        nexus=NexusConfig("https://nexus.example.com", "releases", "com.example", "application"),
        destination=DestinationConfig(tmp_path / "destination"),
        network=NetworkConfig(retries=0, retry_delay_seconds=0), auth=AuthConfig(),
        artifact=ArtifactConfig(),
    )


def payload(version: str, content: bytes, artifact_id: str = "application") -> dict[str, Any]:
    checksum = hashlib.sha256(content).hexdigest()
    filename = f"{artifact_id}-{version}.jar"
    return {
        "items": [{
            "path": f"com/example/{artifact_id}/{version}/{filename}",
            "downloadUrl": f"https://downloads.example.com/{filename}",
            "checksum": {"sha256": checksum},
            "maven2": {"groupId": "com.example", "artifactId": artifact_id, "version": version, "extension": "jar"},
        }],
        "continuationToken": None,
    }


def service(tmp_path: Path, search: SearchSession, download: DownloadSession) -> SyncService:
    return SyncService(
        nexus_client=NexusClient(search), downloader=ArtifactDownloader(download),
        retry_executor=RetryExecutor(sleep=lambda seconds: None),
        state_store_factory=StateStore,
    )


def config(tmp_path: Path) -> AppConfig:
    return AppConfig((target(tmp_path),), state=StateConfig(tmp_path / "state"))


def test_real_components_first_run_then_unchanged_run(tmp_path: Path) -> None:
    body = b"version one"
    first_download = DownloadSession(DownloadResponse(body))
    first = service(tmp_path, SearchSession(payload("1.0", body)), first_download).run(config(tmp_path))
    assert first.results[0].status is TargetSyncStatus.UPDATED
    assert (tmp_path / "destination" / "1.0" / "application-1.0.jar").read_bytes() == body
    state_path = StateStore(tmp_path / "state").path_for("application")
    assert state_path.exists()

    second_download = DownloadSession(DownloadResponse(body))
    second = service(tmp_path, SearchSession(payload("1.0", body)), second_download).run(config(tmp_path))
    assert second.results[0].status is TargetSyncStatus.CURRENT
    assert second_download.calls == 0


def test_real_components_dry_run_has_no_mutating_side_effects(tmp_path: Path) -> None:
    body = b"preview"
    download = DownloadSession(DownloadResponse(body))
    summary = service(tmp_path, SearchSession(payload("2.0", body)), download).run(config(tmp_path), dry_run=True)
    assert summary.results[0].status is TargetSyncStatus.WOULD_UPDATE
    assert download.calls == 0
    assert not (tmp_path / "destination").exists()
    assert not (tmp_path / "state").exists()


def test_existing_matching_artifact_is_adopted_without_download_and_flat_file_is_untouched(
    tmp_path: Path,
) -> None:
    body = b"already deployed"
    destination = tmp_path / "destination"
    deployed = destination / "1.0" / "application-1.0.jar"
    deployed.parent.mkdir(parents=True)
    deployed.write_bytes(body)
    flat = destination / "application-0.9.jar"
    flat.write_bytes(b"legacy flat artifact")
    before = deployed.stat().st_mtime_ns
    download = DownloadSession(DownloadResponse(body))
    summary = service(tmp_path, SearchSession(payload("1.0", body)), download).run(config(tmp_path))
    assert summary.results[0].status is TargetSyncStatus.UPDATED
    assert download.calls == 0
    assert deployed.read_bytes() == body
    assert deployed.stat().st_mtime_ns == before
    assert flat.read_bytes() == b"legacy flat artifact"
    state = StateStore(tmp_path / "state").load("application")
    assert state is not None and Path(state.path) == deployed.resolve()


def test_real_components_new_version_is_append_only_and_updates_state(tmp_path: Path) -> None:
    old, new = b"old version", b"new version"
    service(tmp_path, SearchSession(payload("1.0", old)), DownloadSession(DownloadResponse(old))).run(config(tmp_path))
    summary = service(tmp_path, SearchSession(payload("2.0", new)), DownloadSession(DownloadResponse(new))).run(config(tmp_path))
    assert summary.results[0].status is TargetSyncStatus.UPDATED
    assert (tmp_path / "destination" / "1.0" / "application-1.0.jar").read_bytes() == old
    assert (tmp_path / "destination" / "2.0" / "application-2.0.jar").read_bytes() == new
    state = StateStore(tmp_path / "state").load("application")
    assert state is not None and state.version == "2.0"


def test_real_components_same_version_changed_checksum_is_conflict(tmp_path: Path) -> None:
    old, new = b"old bytes", b"replacement bytes"
    service(tmp_path, SearchSession(payload("1.0", old)), DownloadSession(DownloadResponse(old))).run(config(tmp_path))
    summary = service(tmp_path, SearchSession(payload("1.0", new)), DownloadSession(DownloadResponse(new))).run(config(tmp_path))
    assert summary.results[0].status is TargetSyncStatus.FAILED
    assert (tmp_path / "destination" / "1.0" / "application-1.0.jar").read_bytes() == old
    state = StateStore(tmp_path / "state").load("application")
    assert state is not None and state.checksum == hashlib.sha256(old).hexdigest()


def test_real_components_interrupted_download_preserves_existing_artifact_and_state(tmp_path: Path) -> None:
    original = b"stable"
    service(tmp_path, SearchSession(payload("1.0", original)), DownloadSession(DownloadResponse(original))).run(config(tmp_path))
    state_path = StateStore(tmp_path / "state").path_for("application")
    state_before = state_path.read_bytes()
    failing = DownloadSession(DownloadResponse(b"partial", requests.ConnectionError("secret transport detail")))
    summary = service(tmp_path, SearchSession(payload("2.0", b"partial")), failing).run(config(tmp_path))
    assert summary.results[0].status is TargetSyncStatus.FAILED
    assert (tmp_path / "destination" / "1.0" / "application-1.0.jar").read_bytes() == original
    assert not (tmp_path / "destination" / "2.0" / "application-2.0.jar").exists()
    assert state_path.read_bytes() == state_before
    assert list((tmp_path / "destination").rglob("*.tmp")) == []


def test_real_components_checksum_mismatch_preserves_existing_artifact_and_state(tmp_path: Path) -> None:
    original = b"stable"
    service(tmp_path, SearchSession(payload("1.0", original)), DownloadSession(DownloadResponse(original))).run(config(tmp_path))
    state_path = StateStore(tmp_path / "state").path_for("application")
    state_before = state_path.read_bytes()
    summary = service(
        tmp_path,
        SearchSession(payload("2.0", b"expected")),
        DownloadSession(DownloadResponse(b"corrupt")),
    ).run(config(tmp_path))
    assert summary.results[0].status is TargetSyncStatus.FAILED
    assert (tmp_path / "destination" / "1.0" / "application-1.0.jar").read_bytes() == original
    assert not (tmp_path / "destination" / "2.0" / "application-2.0.jar").exists()
    assert state_path.read_bytes() == state_before
    assert list((tmp_path / "destination").rglob("*.tmp")) == []


def test_real_components_isolate_three_targets_and_preserve_order(tmp_path: Path) -> None:
    base = target(tmp_path)
    targets = tuple(
        replace(
            base,
            id=name,
            nexus=replace(base.nexus, artifact_id=name),
            destination=DestinationConfig(tmp_path / name),
        )
        for name in ("alpha", "bravo", "charlie")
    )
    charlie_body = b"already current"
    charlie_payload = payload("1.0", charlie_body, "charlie")
    charlie_item = charlie_payload["items"][0]
    store = StateStore(tmp_path / "state")
    store.save(
        "charlie",
        TargetState(
            version="1.0", path=str((tmp_path / "charlie" / "1.0" / "charlie-1.0.jar").resolve()), checksum_algorithm="sha256",
            checksum=charlie_item["checksum"]["sha256"], downloaded_at="2026-01-01T00:00:00+00:00",
        ),
    )
    charlie_file = tmp_path / "charlie" / "1.0" / "charlie-1.0.jar"
    charlie_file.parent.mkdir(parents=True)
    charlie_file.write_bytes(charlie_body)
    alpha_body = b"new alpha"
    search = SequentialSearchSession(
        [
            SearchResponse(payload("2.0", alpha_body, "alpha")),
            requests.ConnectionError("transport failed"),
            SearchResponse(charlie_payload),
        ]
    )
    sync = SyncService(
        nexus_client=NexusClient(search),
        downloader=ArtifactDownloader(DownloadSession(DownloadResponse(alpha_body))),
        retry_executor=RetryExecutor(sleep=lambda seconds: None),
        state_store_factory=StateStore,
    )
    summary = sync.run(AppConfig(targets, state=StateConfig(tmp_path / "state")))
    assert [item.target_id for item in summary.results] == ["alpha", "bravo", "charlie"]
    assert [item.status for item in summary.results] == [
        TargetSyncStatus.UPDATED, TargetSyncStatus.FAILED, TargetSyncStatus.CURRENT,
    ]
    assert (tmp_path / "alpha" / "2.0" / "alpha-2.0.jar").read_bytes() == alpha_body
    assert not (tmp_path / "bravo").exists()
    assert charlie_file.read_bytes() == charlie_body
    assert store.load("alpha") is not None
    assert store.load("bravo") is None


def test_two_targets_share_version_directory_and_change_independently(tmp_path: Path) -> None:
    base = target(tmp_path)
    shared = tmp_path / "shared"
    application_id = "application-release-for-a-very-long-independent-target-name-alpha"
    dependencies_id = "application-dependencies-for-a-very-long-independent-target-name-bravo"
    application = replace(base, id=application_id, destination=DestinationConfig(shared))
    dependencies = replace(
        base,
        id=dependencies_id,
        nexus=replace(base.nexus, artifact_id="application-dependencies"),
        destination=DestinationConfig(shared),
    )
    common = b"same-version-content"
    first = SyncService(
        nexus_client=NexusClient(SequentialSearchSession([
            SearchResponse(payload("1.76.0", common, "application")),
            SearchResponse(payload("1.76.0", common, "application-dependencies")),
        ])),
        downloader=ArtifactDownloader(DownloadSession(DownloadResponse(common))),
        retry_executor=RetryExecutor(sleep=lambda seconds: None),
        state_store_factory=StateStore,
    ).run(AppConfig((application, dependencies), state=StateConfig(tmp_path / "state")))
    assert first.updated_count == 2
    assert (shared / "1.76.0" / "application-1.76.0.jar").read_bytes() == common
    assert (shared / "1.76.0" / "application-dependencies-1.76.0.jar").read_bytes() == common
    state_store = StateStore(tmp_path / "state")
    assert state_store.load(application_id) is not None
    assert state_store.load(dependencies_id) is not None
    assert not list((tmp_path / "state").glob(".njs-state-*.tmp"))

    newer = b"new-application"
    second_download = DownloadSession(DownloadResponse(newer))
    second = SyncService(
        nexus_client=NexusClient(SequentialSearchSession([
            SearchResponse(payload("1.77.0", newer, "application")),
            SearchResponse(payload("1.76.0", common, "application-dependencies")),
        ])),
        downloader=ArtifactDownloader(second_download),
        retry_executor=RetryExecutor(sleep=lambda seconds: None),
        state_store_factory=StateStore,
    ).run(AppConfig((application, dependencies), state=StateConfig(tmp_path / "state")))
    assert [result.status for result in second.results] == [
        TargetSyncStatus.UPDATED,
        TargetSyncStatus.CURRENT,
    ]
    assert second_download.calls == 1
    assert (shared / "1.76.0" / "application-1.76.0.jar").read_bytes() == common
    assert (shared / "1.76.0" / "application-dependencies-1.76.0.jar").read_bytes() == common
    assert (shared / "1.77.0" / "application-1.77.0.jar").read_bytes() == newer
    application_state = state_store.load(application_id)
    dependencies_state = state_store.load(dependencies_id)
    assert application_state is not None and application_state.version == "1.77.0"
    assert dependencies_state is not None and dependencies_state.version == "1.76.0"
