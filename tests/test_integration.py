from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator

import requests

from nexus_jar_sync.config import (
    AppConfig, ArtifactConfig, AuthConfig, DestinationConfig, NetworkConfig,
    NexusConfig, RetentionConfig, StateConfig, TargetConfig,
)
from nexus_jar_sync.downloader import ArtifactDownloader
from nexus_jar_sync.lifecycle import ArtifactLifecycleManager
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
        artifact=ArtifactConfig(), retention=RetentionConfig(keep_previous_versions=0),
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
        lifecycle=ArtifactLifecycleManager(), retry_executor=RetryExecutor(sleep=lambda seconds: None),
        state_store_factory=StateStore,
    )


def config(tmp_path: Path) -> AppConfig:
    return AppConfig((target(tmp_path),), state=StateConfig(tmp_path / "state"))


def test_real_components_first_run_then_unchanged_run(tmp_path: Path) -> None:
    body = b"version one"
    first_download = DownloadSession(DownloadResponse(body))
    first = service(tmp_path, SearchSession(payload("1.0", body)), first_download).run(config(tmp_path))
    assert first.results[0].status is TargetSyncStatus.UPDATED
    assert (tmp_path / "destination" / "application-1.0.jar").read_bytes() == body
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


def test_real_components_new_version_applies_retention_and_updates_state(tmp_path: Path) -> None:
    old, new = b"old version", b"new version"
    service(tmp_path, SearchSession(payload("1.0", old)), DownloadSession(DownloadResponse(old))).run(config(tmp_path))
    summary = service(tmp_path, SearchSession(payload("2.0", new)), DownloadSession(DownloadResponse(new))).run(config(tmp_path))
    assert summary.results[0].status is TargetSyncStatus.UPDATED
    assert not (tmp_path / "destination" / "application-1.0.jar").exists()
    assert (tmp_path / "destination" / "application-2.0.jar").read_bytes() == new
    state = StateStore(tmp_path / "state").load("application")
    assert state is not None and state.version == "2.0"


def test_real_components_same_version_changed_checksum_replaces_artifact(tmp_path: Path) -> None:
    old, new = b"old bytes", b"replacement bytes"
    service(tmp_path, SearchSession(payload("1.0", old)), DownloadSession(DownloadResponse(old))).run(config(tmp_path))
    summary = service(tmp_path, SearchSession(payload("1.0", new)), DownloadSession(DownloadResponse(new))).run(config(tmp_path))
    assert summary.results[0].status is TargetSyncStatus.UPDATED
    assert (tmp_path / "destination" / "application-1.0.jar").read_bytes() == new
    state = StateStore(tmp_path / "state").load("application")
    assert state is not None and state.checksum == hashlib.sha256(new).hexdigest()


def test_real_components_interrupted_download_preserves_existing_artifact_and_state(tmp_path: Path) -> None:
    original = b"stable"
    service(tmp_path, SearchSession(payload("1.0", original)), DownloadSession(DownloadResponse(original))).run(config(tmp_path))
    state_path = StateStore(tmp_path / "state").path_for("application")
    state_before = state_path.read_bytes()
    failing = DownloadSession(DownloadResponse(b"partial", requests.ConnectionError("secret transport detail")))
    summary = service(tmp_path, SearchSession(payload("2.0", b"partial")), failing).run(config(tmp_path))
    assert summary.results[0].status is TargetSyncStatus.FAILED
    assert (tmp_path / "destination" / "application-1.0.jar").read_bytes() == original
    assert not (tmp_path / "destination" / "application-2.0.jar").exists()
    assert state_path.read_bytes() == state_before
    assert list((tmp_path / "destination").glob("*.tmp")) == []


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
    assert (tmp_path / "destination" / "application-1.0.jar").read_bytes() == original
    assert not (tmp_path / "destination" / "application-2.0.jar").exists()
    assert state_path.read_bytes() == state_before
    assert list((tmp_path / "destination").glob("*.tmp")) == []


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
            version="1.0", path=charlie_item["path"], checksum_algorithm="sha256",
            checksum=charlie_item["checksum"]["sha256"], downloaded_at="2026-01-01T00:00:00+00:00",
        ),
    )
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
        lifecycle=ArtifactLifecycleManager(), retry_executor=RetryExecutor(sleep=lambda seconds: None),
        state_store_factory=StateStore,
    )
    summary = sync.run(AppConfig(targets, state=StateConfig(tmp_path / "state")))
    assert [item.target_id for item in summary.results] == ["alpha", "bravo", "charlie"]
    assert [item.status for item in summary.results] == [
        TargetSyncStatus.UPDATED, TargetSyncStatus.FAILED, TargetSyncStatus.CURRENT,
    ]
    assert (tmp_path / "alpha" / "alpha-2.0.jar").read_bytes() == alpha_body
    assert not (tmp_path / "bravo").exists()
    assert not (tmp_path / "charlie").exists()
    assert store.load("alpha") is not None
    assert store.load("bravo") is None
