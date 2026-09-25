from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Iterator
import uuid

import pytest
import requests

from nexus_jar_sync.config import (
    ArtifactConfig, AuthConfig, CompanionConfig, DestinationConfig, NetworkConfig,
    NexusConfig, ReleaseArtifactConfig, TargetConfig, ToolsConfig,
)
from nexus_jar_sync.downloader import ArtifactDownloader, DownloadDisposition, DownloadError
from nexus_jar_sync.nexus_client import NexusAsset
from nexus_jar_sync.release import METADATA_NAME, ReleaseAssembler
from nexus_jar_sync.retry import RetryExecutor
from nexus_jar_sync.state import StateStore
from nexus_jar_sync.sync import SyncService, TargetSyncStatus


PRIMARY = b"primary jar"
ARCHIVE = b"synthetic archive"
LICENSE = b"license text"


class Response:
    status_code = 200

    def __init__(self, body: bytes, *, length: int | None = None) -> None:
        self.body = body
        self.headers = {"Content-Length": str(len(body) if length is None else length)}
        self.closed = False

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        yield self.body[:3]
        yield self.body[3:]

    def close(self) -> None:
        self.closed = True


class Session:
    def __init__(self, responses: list[Response | Exception]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> Response:
        self.calls.append((url, kwargs))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class SevenZip:
    def __init__(self, *, unsafe_listing: str | None = None, returncode: int = 0) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.unsafe_listing = unsafe_listing
        self.returncode = returncode

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((command, kwargs))
        if command[1] == "l":
            entry = self.unsafe_listing or "lib/dependency.jar"
            return subprocess.CompletedProcess(command, self.returncode, f"Path = {entry}\n", "")
        output = Path(next(item[2:] for item in command if item.startswith("-o")))
        (output / "lib").mkdir(parents=True)
        (output / "lib" / "dependency.jar").write_bytes(b"dependency")
        return subprocess.CompletedProcess(command, self.returncode, "", "")


def target(destination: Path) -> TargetConfig:
    return TargetConfig(
        id="application-release", enabled=True,
        nexus=NexusConfig("https://nexus.example.invalid", "releases", "org.example", "application"),
        destination=DestinationConfig(destination), network=NetworkConfig(timeout_seconds=17),
        auth=AuthConfig(username="user", password="secret"), artifact=ArtifactConfig(),
        companions=(
            CompanionConfig("dependencies", "https://nexus.example.invalid/static/dependencies.7z", "dependencies.7z", "extract_7z"),
            CompanionConfig("license", "https://nexus.example.invalid/static/license.txt", "license.txt", "copy"),
        ),
    )


def asset(version: str = "1.0", content: bytes = PRIMARY) -> NexusAsset:
    filename = f"application-{version}.jar"
    return NexusAsset(version, filename, f"https://nexus.example.invalid/{filename}", f"org/example/{version}/{filename}", {"sha256": hashlib.sha256(content).hexdigest()})


def artifact_asset(artifact_id: str, version: str, content: bytes) -> NexusAsset:
    filename = f"{artifact_id}-{version}.jar"
    return NexusAsset(
        version, filename, f"https://nexus.example.invalid/{filename}",
        f"org/example/{artifact_id}/{version}/{filename}",
        {"sha256": hashlib.sha256(content).hexdigest()},
    )


def assembler(primary: Session, companions: Session, runner: SevenZip) -> ReleaseAssembler:
    return ReleaseAssembler(ArtifactDownloader(primary), companions, command_runner=runner)


def test_complete_release_is_published_then_reused_without_companion_get_or_7z(tmp_path: Path) -> None:
    primary = Session([Response(PRIMARY)])
    companion = Session([Response(ARCHIVE), Response(LICENSE)])
    seven_zip = SevenZip()
    release = assembler(primary, companion, seven_zip)
    configured = target(tmp_path / "destination")
    tools = ToolsConfig(Path("C:/Program Files/7-Zip/7z.exe"), 30)
    first = release.assemble(asset(), configured, tools)
    root = tmp_path / "destination" / "1.0"
    assert first.path == root / "application-1.0.jar"
    assert (root / "dependencies.7z").read_bytes() == ARCHIVE
    assert (root / "license.txt").read_bytes() == LICENSE
    assert (root / "lib" / "dependency.jar").read_bytes() == b"dependency"
    assert (root / METADATA_NAME).is_file()
    assert all(call[1]["shell"] is False for call in seven_zip.calls)
    assert all("-aoa" not in call[0] for call in seven_zip.calls)
    changed_companions = replace(
        configured,
        companions=tuple(
            replace(item, url=item.url + "?changed=true") for item in configured.companions
        ),
    )
    second = release.assemble(asset(), changed_companions, tools)
    assert second.disposition is DownloadDisposition.REUSED
    assert len(primary.calls) == 1
    assert len(companion.calls) == 2
    assert len(seven_zip.calls) == 2


def test_primary_version_publishes_four_maven_artifacts_atomically_and_reuses(
    tmp_path: Path,
) -> None:
    bodies = {
        "windows-obs": b"windows obs", "linux-versions": b"linux",
        "linux-obs": b"linux obs",
    }
    configured = replace(
        target(tmp_path / "destination"),
        nexus=NexusConfig("https://nexus.example.invalid", "releases", "org.example", "windows-versions"),
        companions=(),
        release_artifacts=(
            ReleaseArtifactConfig("windows-obfuscated", "windows-obs"),
            ReleaseArtifactConfig("linux", "linux-versions"),
            ReleaseArtifactConfig("linux-obfuscated", "linux-obs"),
        ),
    )
    primary_body = b"windows versions"
    selected = artifact_asset("windows-versions", "1.76.0", primary_body)
    downloads = Session([Response(primary_body), *(Response(body) for body in bodies.values())])
    release = assembler(downloads, Session([]), SevenZip())
    resolved: list[str] = []

    def resolve(artifact_id: str) -> NexusAsset:
        resolved.append(artifact_id)
        return artifact_asset(artifact_id, "1.76.0", bodies[artifact_id])

    first = release.assemble(selected, configured, ToolsConfig(), secondary_resolver=resolve)
    root = first.path.parent
    assert sorted(path.name for path in root.glob("*.jar")) == [
        "linux-obs-1.76.0.jar", "linux-versions-1.76.0.jar",
        "windows-obs-1.76.0.jar", "windows-versions-1.76.0.jar",
    ]
    metadata = json.loads((root / METADATA_NAME).read_text(encoding="utf-8"))
    assert [item["role"] for item in metadata["artifacts"]] == [
        "primary", "secondary", "secondary", "secondary"
    ]
    assert sum(item["role"] in {"primary", "secondary"} for item in metadata["files"]) == 4
    timestamps = {path: path.stat().st_mtime_ns for path in root.iterdir()}
    second = release.assemble(
        selected, configured, ToolsConfig(),
        secondary_resolver=lambda artifact_id: pytest.fail("unchanged release resolved secondary"),
    )
    assert second.disposition is DownloadDisposition.REUSED
    assert resolved == ["windows-obs", "linux-versions", "linux-obs"]
    assert timestamps == {path: path.stat().st_mtime_ns for path in root.iterdir()}


def test_secondary_failure_never_publishes_partial_release(tmp_path: Path) -> None:
    configured = replace(
        target(tmp_path / "destination"), companions=(),
        release_artifacts=(ReleaseArtifactConfig("missing", "missing-artifact"),),
    )
    with pytest.raises(DownloadError, match="missing secondary"):
        assembler(Session([Response(PRIMARY)]), Session([]), SevenZip()).assemble(
            asset(), configured, ToolsConfig(),
            secondary_resolver=lambda artifact_id: (_ for _ in ()).throw(DownloadError("missing secondary")),
        )
    assert not (tmp_path / "destination" / "1.0").exists()


def test_secondary_checksum_failure_never_publishes_partial_release(tmp_path: Path) -> None:
    configured = replace(
        target(tmp_path / "destination"), companions=(),
        release_artifacts=(ReleaseArtifactConfig("secondary", "other"),),
    )
    release = assembler(
        Session([Response(PRIMARY), Response(b"wrong secondary bytes")]),
        Session([]), SevenZip(),
    )
    with pytest.raises(DownloadError, match="checksum does not match"):
        release.assemble(
            asset(), configured, ToolsConfig(),
            secondary_resolver=lambda artifact_id: artifact_asset(
                artifact_id, "1.0", b"expected secondary bytes"
            ),
        )
    assert not (tmp_path / "destination" / "1.0").exists()


def test_secondary_tampering_invalidates_completed_release(tmp_path: Path) -> None:
    configured = replace(
        target(tmp_path / "destination"), companions=(),
        release_artifacts=(ReleaseArtifactConfig("secondary", "other"),),
    )
    release = assembler(Session([Response(PRIMARY), Response(b"secondary")]), Session([]), SevenZip())
    release.assemble(
        asset(), configured, ToolsConfig(),
        secondary_resolver=lambda artifact_id: artifact_asset(artifact_id, "1.0", b"secondary"),
    )
    secondary = tmp_path / "destination" / "1.0" / "other-1.0.jar"
    secondary.write_bytes(b"tampered")
    with pytest.raises(DownloadError, match="integrity"):
        release.assemble(asset(), configured, ToolsConfig(), secondary_resolver=lambda _: pytest.fail())


def test_new_primary_version_downloads_fresh_companions_and_retains_old_release(tmp_path: Path) -> None:
    primary = Session([Response(PRIMARY), Response(b"new primary")])
    companions = Session([Response(ARCHIVE), Response(LICENSE), Response(b"new archive"), Response(b"new license")])
    release = assembler(primary, companions, SevenZip())
    configured = target(tmp_path / "destination")
    tools = ToolsConfig(Path("7z.exe"), 30)
    release.assemble(asset(), configured, tools)
    release.assemble(asset("2.0", b"new primary"), configured, tools)
    assert (tmp_path / "destination" / "1.0" / "application-1.0.jar").read_bytes() == PRIMARY
    assert (tmp_path / "destination" / "2.0" / "application-2.0.jar").read_bytes() == b"new primary"
    assert len(companions.calls) == 4


def test_companion_request_options_are_get_only_and_redirect_is_rejected(tmp_path: Path) -> None:
    redirect = Response(ARCHIVE)
    redirect.status_code = 302
    primary = Session([Response(PRIMARY)])
    companions = Session([redirect])
    with pytest.raises(DownloadError, match="redirect rejected"):
        assembler(primary, companions, SevenZip()).assemble(
            asset(), target(tmp_path / "destination"), ToolsConfig(Path("7z.exe"), 30)
        )
    _, options = companions.calls[0]
    assert options == {"auth": ("user", "secret"), "timeout": 17, "verify": True, "stream": True, "allow_redirects": False}
    assert redirect.closed
    assert not (tmp_path / "destination" / "1.0").exists()


@pytest.mark.parametrize("entry", ["../escape", "/absolute", "C:/drive", "\\\\server\\share", "file:stream", "CON", "trail.", "bad\x01name"])
def test_unsafe_archive_listing_never_publishes(tmp_path: Path, entry: str) -> None:
    with pytest.raises(DownloadError, match="Unsafe archive entry"):
        assembler(Session([Response(PRIMARY)]), Session([Response(ARCHIVE)]), SevenZip(unsafe_listing=entry)).assemble(
            asset(), replace(target(tmp_path / "destination"), companions=target(tmp_path).companions[:1]), ToolsConfig(Path("7z.exe"), 30)
        )
    assert not (tmp_path / "destination" / "1.0").exists()


def test_companion_length_failure_closes_response_and_cleans_stage(tmp_path: Path) -> None:
    response = Response(ARCHIVE, length=len(ARCHIVE) + 1)
    with pytest.raises(DownloadError) as caught:
        assembler(Session([Response(PRIMARY)]), Session([response]), SevenZip()).assemble(
            asset(), replace(target(tmp_path / "destination"), companions=target(tmp_path).companions[:1]), ToolsConfig(Path("7z.exe"), 30)
        )
    assert caught.value.retryable
    assert response.closed
    assert not list((tmp_path / "destination").glob(".*-release-*"))


def test_same_version_checksum_change_is_conflict_without_overwrite(tmp_path: Path) -> None:
    configured = replace(target(tmp_path / "destination"), companions=())
    release = assembler(Session([Response(PRIMARY)]), Session([]), SevenZip())
    release.assemble(asset(), configured, ToolsConfig())
    with pytest.raises(DownloadError, match="metadata conflict|checksum conflict"):
        release.assemble(asset(content=b"changed"), configured, ToolsConfig())
    assert (tmp_path / "destination" / "1.0" / "application-1.0.jar").read_bytes() == PRIMARY


def test_7z_nonzero_and_timeout_are_sanitized(tmp_path: Path) -> None:
    configured = replace(target(tmp_path / "destination"), companions=target(tmp_path).companions[:1])
    with pytest.raises(DownloadError, match="7-Zip failed"):
        assembler(Session([Response(PRIMARY)]), Session([Response(ARCHIVE)]), SevenZip(returncode=2)).assemble(asset(), configured, ToolsConfig(Path("7z.exe"), 30))

    def timeout(command: list[str], **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    with pytest.raises(DownloadError, match="timed out"):
        ReleaseAssembler(ArtifactDownloader(Session([Response(PRIMARY)])), Session([Response(ARCHIVE)]), command_runner=timeout).assemble(
            asset(), replace(configured, destination=DestinationConfig(tmp_path / "other")), ToolsConfig(Path("7z.exe"), 1)
        )


def test_sync_primary_only_trigger_saves_state_after_complete_release(tmp_path: Path) -> None:
    class Client:
        def get_latest_asset(self, configured: TargetConfig) -> NexusAsset:
            return asset()

    primary_session = Session([Response(PRIMARY)])
    companion_session = Session([Response(ARCHIVE), Response(LICENSE)])
    runner = SevenZip()
    primary_downloader = ArtifactDownloader(primary_session)
    releases = ReleaseAssembler(primary_downloader, companion_session, command_runner=runner)
    service = SyncService(
        nexus_client=Client(),  # type: ignore[arg-type]
        downloader=primary_downloader,
        release_assembler=releases,
        retry_executor=RetryExecutor(sleep=lambda seconds: None),
        state_store_factory=StateStore,
    )
    configured = target(tmp_path / "destination")
    from nexus_jar_sync.config import AppConfig, StateConfig

    config = AppConfig((configured,), state=StateConfig(tmp_path / "state"), tools=ToolsConfig(Path("7z.exe"), 30))
    first = service.run(config)
    second = service.run(config)
    assert first.results[0].status is TargetSyncStatus.UPDATED
    assert second.results[0].status is TargetSyncStatus.CURRENT
    assert len(primary_session.calls) == 1
    assert len(companion_session.calls) == 2
    assert len(runner.calls) == 2
    state = StateStore(tmp_path / "state").load(configured.id)
    assert state is not None
    assert state.release_directory == str((tmp_path / "destination" / "1.0").resolve())
    assert state.metadata_path == str((tmp_path / "destination" / "1.0" / METADATA_NAME).resolve())


def test_sync_resolves_secondaries_only_for_new_primary_and_saves_state_last(tmp_path: Path) -> None:
    bodies = {"one": b"one", "two": b"two", "three": b"three"}

    class Client:
        def __init__(self) -> None:
            self.secondary_calls: list[tuple[str, str]] = []

        def get_latest_asset(self, configured: TargetConfig) -> NexusAsset:
            return asset("4.0", PRIMARY)

        def get_asset_at_version(
            self, configured: TargetConfig, artifact_id: str, version: str
        ) -> NexusAsset:
            self.secondary_calls.append((artifact_id, version))
            return artifact_asset(artifact_id, version, bodies[artifact_id])

    client = Client()
    configured = replace(
        target(tmp_path / "destination"), companions=(),
        release_artifacts=(
            ReleaseArtifactConfig("one", "one"), ReleaseArtifactConfig("two", "two"),
            ReleaseArtifactConfig("three", "three"),
        ),
    )
    downloads = Session([Response(PRIMARY), Response(b"one"), Response(b"two"), Response(b"three")])
    primary = ArtifactDownloader(downloads)
    service = SyncService(
        nexus_client=client,  # type: ignore[arg-type]
        downloader=primary,
        release_assembler=ReleaseAssembler(primary, Session([]), command_runner=SevenZip()),
        retry_executor=RetryExecutor(sleep=lambda seconds: None),
        state_store_factory=StateStore,
    )
    from nexus_jar_sync.config import AppConfig, StateConfig
    config = AppConfig((configured,), state=StateConfig(tmp_path / "state"))
    assert service.run(config).results[0].status is TargetSyncStatus.UPDATED
    assert service.run(config).results[0].status is TargetSyncStatus.CURRENT
    assert client.secondary_calls == [("one", "4.0"), ("two", "4.0"), ("three", "4.0")]
    assert len(downloads.calls) == 4
    state = StateStore(tmp_path / "state").load(configured.id)
    assert state is not None and state.version == "4.0"


def test_isolated_test_download_ignores_state_and_writes_only_under_test_root(tmp_path: Path) -> None:
    class Client:
        def get_latest_asset(self, configured: TargetConfig) -> NexusAsset:
            return asset()

    primary = ArtifactDownloader(Session([Response(PRIMARY)]))
    releases = ReleaseAssembler(primary, Session([Response(ARCHIVE), Response(LICENSE)]), command_runner=SevenZip())
    service = SyncService(
        nexus_client=Client(),  # type: ignore[arg-type]
        downloader=primary,
        release_assembler=releases,
        retry_executor=RetryExecutor(sleep=lambda seconds: None),
        state_store_factory=lambda path: pytest.fail("test mode must not construct state"),
    )
    configured = target(tmp_path / "production")
    from nexus_jar_sync.config import AppConfig, StateConfig

    output = tmp_path / "test-output"
    output.mkdir()
    summary = service.run_test_download(
        AppConfig((configured,), state=StateConfig(tmp_path / "production-state"), tools=ToolsConfig(Path("7z.exe"), 30)),
        output,
    )
    assert summary.results[0].status is TargetSyncStatus.UPDATED
    assert (output / configured.id / "1.0" / "application-1.0.jar").is_file()
    assert not configured.destination.directory.exists()
    assert not (tmp_path / "production-state").exists()


def test_isolated_test_download_builds_four_artifact_flattened_release(tmp_path: Path) -> None:
    primary_body = b"windows versions"
    secondary_bodies = {
        "windows-obs": b"windows obs",
        "linux-versions": b"linux versions",
        "linux-obs": b"linux obs",
    }

    class Client:
        def __init__(self) -> None:
            self.latest_calls = 0
            self.secondary_calls: list[tuple[str, str]] = []

        def get_latest_asset(self, configured: TargetConfig) -> NexusAsset:
            self.latest_calls += 1
            return artifact_asset("windows-versions", "1.76.0", primary_body)

        def get_asset_at_version(
            self, configured: TargetConfig, artifact_id: str, version: str
        ) -> NexusAsset:
            self.secondary_calls.append((artifact_id, version))
            return artifact_asset(artifact_id, version, secondary_bodies[artifact_id])

    class WrapperSevenZip(SevenZip):
        def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            self.calls.append((command, kwargs))
            if command[1] == "l":
                return subprocess.CompletedProcess(
                    command, 0, "Path = dependency-wrapper/lib/runtime.jar\n", ""
                )
            output = Path(next(item[2:] for item in command if item.startswith("-o")))
            (output / "dependency-wrapper" / "lib").mkdir(parents=True)
            (output / "dependency-wrapper" / "lib" / "runtime.jar").write_bytes(
                b"runtime dependency"
            )
            return subprocess.CompletedProcess(command, 0, "", "")

    client = Client()
    download_session = Session([
        Response(primary_body),
        Response(secondary_bodies["windows-obs"]),
        Response(secondary_bodies["linux-versions"]),
        Response(secondary_bodies["linux-obs"]),
    ])
    companion_session = Session([Response(ARCHIVE), Response(LICENSE)])
    runner = WrapperSevenZip()
    downloader = ArtifactDownloader(download_session)
    service = SyncService(
        nexus_client=client,  # type: ignore[arg-type]
        downloader=downloader,
        release_assembler=ReleaseAssembler(
            downloader, companion_session, command_runner=runner
        ),
        retry_executor=RetryExecutor(sleep=lambda seconds: None),
        state_store_factory=lambda path: pytest.fail("test mode must not construct state"),
    )
    configured = replace(
        target(tmp_path / "production"),
        nexus=NexusConfig(
            "https://nexus.example.invalid", "releases", "org.example", "windows-versions"
        ),
        release_artifacts=(
            ReleaseArtifactConfig("windows-obfuscated", "windows-obs"),
            ReleaseArtifactConfig("linux", "linux-versions"),
            ReleaseArtifactConfig("linux-obfuscated", "linux-obs"),
        ),
        companions=(
            replace(
                target(tmp_path).companions[0],
                keep_archive=False,
                strip_single_root=True,
            ),
            target(tmp_path).companions[1],
        ),
    )
    from nexus_jar_sync.config import AppConfig, StateConfig

    output = tmp_path / "test-output"
    output.mkdir()
    summary = service.run_test_download(
        AppConfig(
            (configured,),
            state=StateConfig(tmp_path / "production-state"),
            tools=ToolsConfig(Path("7z.exe"), 30),
        ),
        output,
    )

    assert summary.results[0].status is TargetSyncStatus.UPDATED
    root = output / configured.id / "1.76.0"
    assert sorted(path.name for path in root.glob("*.jar")) == [
        "linux-obs-1.76.0.jar",
        "linux-versions-1.76.0.jar",
        "windows-obs-1.76.0.jar",
        "windows-versions-1.76.0.jar",
    ]
    assert not (root / "dependencies.7z").exists()
    assert not (root / "dependency-wrapper").exists()
    assert (root / "lib" / "runtime.jar").read_bytes() == b"runtime dependency"
    assert (root / "license.txt").read_bytes() == LICENSE
    metadata = json.loads((root / METADATA_NAME).read_text(encoding="utf-8"))
    assert [record["artifact_id"] for record in metadata["artifacts"]] == [
        "windows-versions", "windows-obs", "linux-versions", "linux-obs"
    ]
    assert {record["path"] for record in metadata["files"]} == {
        "windows-versions-1.76.0.jar", "windows-obs-1.76.0.jar",
        "linux-versions-1.76.0.jar", "linux-obs-1.76.0.jar",
        "license.txt", "lib/runtime.jar",
    }
    assert client.latest_calls == 1
    assert client.secondary_calls == [
        ("windows-obs", "1.76.0"),
        ("linux-versions", "1.76.0"),
        ("linux-obs", "1.76.0"),
    ]
    assert len(download_session.calls) == 4
    assert len(companion_session.calls) == 2
    assert not configured.destination.directory.exists()
    assert not (tmp_path / "production-state").exists()


def test_extracted_file_collision_with_companion_aborts_publication(tmp_path: Path) -> None:
    class CollisionSevenZip(SevenZip):
        def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            self.calls.append((command, kwargs))
            if command[1] == "l":
                return subprocess.CompletedProcess(command, 0, "Path = license.txt\n", "")
            output = Path(next(item[2:] for item in command if item.startswith("-o")))
            output.mkdir(exist_ok=True)
            (output / "license.txt").write_bytes(b"from archive")
            return subprocess.CompletedProcess(command, 0, "", "")

    with pytest.raises(DownloadError, match="collision"):
        assembler(Session([Response(PRIMARY)]), Session([Response(ARCHIVE), Response(LICENSE)]), CollisionSevenZip()).assemble(
            asset(), target(tmp_path / "destination"), ToolsConfig(Path("7z.exe"), 30)
        )
    assert not (tmp_path / "destination" / "1.0").exists()


def test_keep_archive_false_omits_only_staged_archive(tmp_path: Path) -> None:
    configured = replace(
        target(tmp_path / "destination"),
        companions=(replace(target(tmp_path).companions[0], keep_archive=False),),
    )
    assembler(Session([Response(PRIMARY)]), Session([Response(ARCHIVE)]), SevenZip()).assemble(
        asset(), configured, ToolsConfig(Path("7z.exe"), 30)
    )
    root = tmp_path / "destination" / "1.0"
    assert not (root / "dependencies.7z").exists()
    assert (root / "lib" / "dependency.jar").is_file()
    assert (root / METADATA_NAME).is_file()
    metadata = json.loads((root / METADATA_NAME).read_text(encoding="utf-8"))
    assert [item["path"] for item in metadata["files"]] == [
        "application-1.0.jar", "lib/dependency.jar"
    ]
    assert [item["role"] for item in metadata["files"]] == ["primary", "extracted"]


@pytest.mark.parametrize("keep_archive", [True, False])
def test_strip_single_root_publishes_only_wrapper_children(
    tmp_path: Path, keep_archive: bool
) -> None:
    class WrapperSevenZip(SevenZip):
        def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            self.calls.append((command, kwargs))
            if command[1] == "l":
                return subprocess.CompletedProcess(command, 0, "Path = wrapper/lib/dependency.jar\n", "")
            output = Path(next(item[2:] for item in command if item.startswith("-o")))
            (output / "wrapper" / "lib").mkdir(parents=True)
            (output / "wrapper" / "lib" / "dependency.jar").write_bytes(b"dependency")
            return subprocess.CompletedProcess(command, 0, "", "")

    companion = replace(
        target(tmp_path).companions[0], keep_archive=keep_archive, strip_single_root=True
    )
    configured = replace(target(tmp_path / "destination"), companions=(companion,))
    assembler(Session([Response(PRIMARY)]), Session([Response(ARCHIVE)]), WrapperSevenZip()).assemble(
        asset(), configured, ToolsConfig(Path("7z.exe"), 30)
    )
    root = tmp_path / "destination" / "1.0"
    assert (root / "lib" / "dependency.jar").read_bytes() == b"dependency"
    assert not (root / "wrapper").exists()
    assert (root / "dependencies.7z").exists() is keep_archive


@pytest.mark.parametrize("shape", ["siblings", "sibling_file", "file", "empty"])
def test_strip_single_root_rejects_invalid_top_level_shape(tmp_path: Path, shape: str) -> None:
    class InvalidWrapper(SevenZip):
        def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if command[1] == "l":
                return subprocess.CompletedProcess(command, 0, "Path = wrapper/file.txt\n", "")
            output = Path(next(item[2:] for item in command if item.startswith("-o")))
            if shape == "file":
                output.mkdir(exist_ok=True); (output / "file.txt").write_text("x")
            else:
                (output / "wrapper").mkdir(parents=True)
                if shape == "siblings":
                    (output / "other").mkdir()
                elif shape == "sibling_file":
                    (output / "sibling.txt").write_text("not inside wrapper")
            return subprocess.CompletedProcess(command, 0, "", "")

    companion = replace(target(tmp_path).companions[0], strip_single_root=True)
    configured = replace(target(tmp_path / "destination"), companions=(companion,))
    with pytest.raises(DownloadError, match="wrapper"):
        assembler(Session([Response(PRIMARY)]), Session([Response(ARCHIVE)]), InvalidWrapper()).assemble(
            asset(), configured, ToolsConfig(Path("7z.exe"), 30)
        )
    assert not (tmp_path / "destination" / "1.0").exists()


def test_strip_single_root_rejects_case_insensitive_flattening_collision(
    tmp_path: Path,
) -> None:
    class CaseCollisionWrapper(SevenZip):
        def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if command[1] == "l":
                return subprocess.CompletedProcess(
                    command, 0, "Path = wrapper/LICENSE.TXT\n", ""
                )
            output = Path(next(item[2:] for item in command if item.startswith("-o")))
            (output / "wrapper").mkdir(parents=True)
            (output / "wrapper" / "LICENSE.TXT").write_text("collision")
            return subprocess.CompletedProcess(command, 0, "", "")

    companion = replace(target(tmp_path).companions[0], strip_single_root=True)
    # Copy the lower-case name first, then flatten an upper-case equivalent.
    configured = replace(
        target(tmp_path / "destination"),
        companions=(target(tmp_path).companions[1], companion),
    )
    with pytest.raises(DownloadError, match="collision"):
        assembler(
            Session([Response(PRIMARY)]), Session([Response(LICENSE), Response(ARCHIVE)]),
            CaseCollisionWrapper(),
        ).assemble(asset(), configured, ToolsConfig(Path("7z.exe"), 30))
    assert not (tmp_path / "destination" / "1.0").exists()


@pytest.mark.parametrize("mutation", ["removed", "modified", "unexpected"])
def test_completed_release_inventory_detects_tree_changes(tmp_path: Path, mutation: str) -> None:
    configured = replace(target(tmp_path / "destination"), companions=())
    release = assembler(Session([Response(PRIMARY)]), Session([]), SevenZip())
    release.assemble(asset(), configured, ToolsConfig())
    root = tmp_path / "destination" / "1.0"
    primary = root / "application-1.0.jar"
    if mutation == "removed":
        primary.unlink()
    elif mutation == "modified":
        primary.write_bytes(b"modified")
    else:
        (root / "unexpected.txt").write_text("unexpected", encoding="utf-8")
    with pytest.raises(DownloadError, match="inventory mismatch|integrity check|checksum conflict"):
        release.assemble(asset(), configured, ToolsConfig())


@pytest.mark.parametrize("mutation", ["duplicate", "malformed", "unsafe"])
def test_completed_release_rejects_invalid_inventory_metadata(tmp_path: Path, mutation: str) -> None:
    configured = replace(target(tmp_path / "destination"), companions=())
    release = assembler(Session([Response(PRIMARY)]), Session([]), SevenZip())
    release.assemble(asset(), configured, ToolsConfig())
    metadata_path = tmp_path / "destination" / "1.0" / METADATA_NAME
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if mutation == "duplicate":
        metadata["files"].append(dict(metadata["files"][0]))
    elif mutation == "malformed":
        metadata["files"] = {"not": "a list"}
    else:
        metadata["files"][0]["path"] = "../escape.jar"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(DownloadError, match="metadata|duplicate"):
        release.assemble(asset(), configured, ToolsConfig())


def test_publication_failure_leaves_no_final_tree_and_retry_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = replace(target(tmp_path / "destination"), companions=())
    primary = Session([Response(PRIMARY), Response(PRIMARY)])
    release = assembler(primary, Session([]), SevenZip())
    original_rename = os.rename
    failed = False

    def fail_once(source: Path, destination: Path) -> None:
        nonlocal failed
        if Path(source).name == "1.0" and not failed:
            failed = True
            raise OSError("injected private detail")
        original_rename(source, destination)

    monkeypatch.setattr("nexus_jar_sync.release.os.rename", fail_once)
    with pytest.raises(DownloadError, match="Could not publish release"):
        release.assemble(asset(), configured, ToolsConfig())
    assert not (tmp_path / "destination" / "1.0").exists()
    result = release.assemble(asset(), configured, ToolsConfig())
    assert result.path.is_file()
    assert not list((tmp_path / "destination").glob(".njs-publish-*.lock"))


def test_publication_race_reuses_identical_complete_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    configured = replace(target(tmp_path / "destination"), companions=())
    release = assembler(Session([Response(PRIMARY)]), Session([]), SevenZip())

    def winner_then_fail(source: Path, destination: Path) -> None:
        shutil.copytree(source, destination)
        raise OSError("lost publication race")

    monkeypatch.setattr("nexus_jar_sync.release.os.rename", winner_then_fail)
    result = release.assemble(asset(), configured, ToolsConfig())
    assert result.disposition is DownloadDisposition.REUSED
    assert result.path.read_bytes() == PRIMARY


def test_publication_race_rejects_conflicting_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    configured = replace(target(tmp_path / "destination"), companions=())
    release = assembler(Session([Response(PRIMARY)]), Session([]), SevenZip())

    def conflicting_winner(source: Path, destination: Path) -> None:
        shutil.copytree(source, destination)
        (destination / "application-1.0.jar").write_bytes(b"conflict")
        raise OSError("lost publication race")

    monkeypatch.setattr("nexus_jar_sync.release.os.rename", conflicting_winner)
    with pytest.raises(DownloadError, match="conflict|integrity"):
        release.assemble(asset(), configured, ToolsConfig())
    assert (tmp_path / "destination" / "1.0" / "application-1.0.jar").read_bytes() == b"conflict"


def test_short_private_names_avoid_old_windows_staging_path_expansion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    short_root = Path(tempfile.gettempdir()).resolve()
    stem = ".njs-release-path-" + uuid.uuid4().hex + "-"
    filler_length = 190 - len(str(short_root)) - 1 - len(stem)
    if filler_length < 1 or filler_length > 120:
        pytest.skip("host temporary root cannot represent the controlled path budget")
    base = short_root / (stem + "x" * filler_length)
    configured = replace(target(base), id="target-" + "x" * 140, companions=())
    selected = asset("1.0", PRIMARY)
    assert len(str(base / "1.0" / selected.filename)) < 240
    assert len(str(base / ("." + configured.id + "-release-xxxxxxxx") / "1.0" / selected.filename)) >= 240
    monkeypatch.setattr("nexus_jar_sync.release.sys.platform", "win32")
    try:
        result = assembler(Session([Response(PRIMARY)]), Session([]), SevenZip()).assemble(
            selected, configured, ToolsConfig()
        )
        assert result.path == base / "1.0" / selected.filename
    finally:
        import shutil
        shutil.rmtree(base, ignore_errors=True)


def test_unsupported_windows_final_path_fails_before_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / ("x" * 210)
    session = Session([Response(PRIMARY)])
    monkeypatch.setattr("nexus_jar_sync.release.sys.platform", "win32")
    with pytest.raises(DownloadError, match="too long"):
        assembler(session, Session([]), SevenZip()).assemble(
            asset(), replace(target(base), companions=()), ToolsConfig()
        )
    assert not session.calls
