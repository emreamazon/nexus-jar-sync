from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import subprocess
from typing import Any, Iterator

import pytest
import requests

from nexus_jar_sync.config import (
    ArtifactConfig, AuthConfig, CompanionConfig, DestinationConfig, NetworkConfig,
    NexusConfig, TargetConfig, ToolsConfig,
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
