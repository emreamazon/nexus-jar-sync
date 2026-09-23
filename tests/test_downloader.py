from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterator

import pytest
import requests

import nexus_jar_sync.downloader as downloader_module
from nexus_jar_sync.config import (
    ArtifactConfig,
    AuthConfig,
    DestinationConfig,
    NetworkConfig,
    NexusConfig,
    RetentionConfig,
    TargetConfig,
)
from nexus_jar_sync.downloader import ArtifactDownloader, DownloadError
from nexus_jar_sync.nexus_client import NexusAsset


CONTENT = b"first chunk-second chunk"


class FakeResponse:
    def __init__(
        self,
        chunks: list[bytes] | None = None,
        *,
        status_code: int = 200,
        headers: Any = None,
        stream_error: Exception | None = None,
    ) -> None:
        self.chunks = chunks if chunks is not None else [CONTENT]
        self.status_code = status_code
        self.headers = {} if headers is None else headers
        self.stream_error = stream_error
        self.closed = False

    @property
    def content(self) -> bytes:
        raise AssertionError("downloader must not load the full response")

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        assert chunk_size == ArtifactDownloader.CHUNK_SIZE
        for chunk in self.chunks:
            yield chunk
        if self.stream_error is not None:
            raise self.stream_error

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, result: FakeResponse | Exception) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def make_target(
    destination: Path,
    *,
    classifier: str | None = None,
    extension: str = "jar",
    username: str | None = None,
    password: str | None = None,
    verify_tls: bool = True,
    ca_bundle: Path | None = None,
) -> TargetConfig:
    return TargetConfig(
        id="example",
        enabled=True,
        nexus=NexusConfig(
            url="https://nexus.example.com",
            repository="releases",
            group_id="com.example",
            artifact_id="application",
        ),
        destination=DestinationConfig(destination),
        network=NetworkConfig(timeout_seconds=17, verify_tls=verify_tls, ca_bundle=ca_bundle),
        auth=AuthConfig(username=username, password=password),
        artifact=ArtifactConfig(extension=extension, classifier=classifier),
        retention=RetentionConfig(),
    )


def make_asset(
    *,
    content: bytes = CONTENT,
    algorithm: str = "sha256",
    checksum: str | None = None,
    filename: str = "application-1.0.0.jar",
    version: str = "1.0.0",
) -> NexusAsset:
    digest = checksum if checksum is not None else hashlib.new(algorithm, content).hexdigest()
    return NexusAsset(
        version=version,
        filename=filename,
        download_url="https://downloads.example.net/application-1.0.0.jar",
        path=f"com/example/application/{version}/{filename}",
        checksums={algorithm: digest},
    )


def temporary_downloads(destination: Path) -> list[Path]:
    return list(destination.glob("*.download.tmp"))


def test_request_options_streaming_and_result(tmp_path: Path) -> None:
    response = FakeResponse([b"first chunk", b"", b"-second chunk"])
    session = FakeSession(response)
    target = make_target(
        tmp_path / "destination",
        username="alice",
        password="secret",
        verify_tls=False,
    )
    assert not target.destination.directory.exists()

    result = ArtifactDownloader(session).download(make_asset(), target)

    assert target.destination.directory.is_dir()
    assert session.calls == [
        (
            "https://downloads.example.net/application-1.0.0.jar",
            {
                "auth": ("alice", "secret"),
                "timeout": 17,
                "verify": False,
                "stream": True,
            },
        )
    ]
    assert result.path.read_bytes() == CONTENT
    assert result.bytes_written == len(CONTENT)
    assert result.checksum_algorithm == "sha256"
    assert result.checksum == hashlib.sha256(CONTENT).hexdigest()
    assert response.closed
    assert not temporary_downloads(target.destination.directory)


def test_no_auth_when_credentials_are_absent(tmp_path: Path) -> None:
    session = FakeSession(FakeResponse())
    ArtifactDownloader(session).download(make_asset(), make_target(tmp_path))
    assert session.calls[0][1]["auth"] is None


def test_ca_bundle_overrides_tls_boolean(tmp_path: Path) -> None:
    session = FakeSession(FakeResponse())
    ArtifactDownloader(session).download(
        make_asset(),
        make_target(tmp_path, verify_tls=False, ca_bundle=Path("certificates/company.pem")),
    )
    assert session.calls[0][1]["verify"] in {
        "certificates/company.pem",
        "certificates\\company.pem",
    }


def test_destination_directory_failure_is_translated_before_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "destination"
    session = FakeSession(FakeResponse())
    original_mkdir = Path.mkdir

    def fail_destination(path: Path, *args: object, **kwargs: object) -> None:
        if path == destination:
            raise OSError("private filesystem detail")
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_destination)
    with pytest.raises(DownloadError, match="Could not create destination") as caught:
        ArtifactDownloader(session).download(make_asset(), make_target(destination))
    assert caught.value.retryable is False
    assert not session.calls


def test_temporary_file_creation_failure_closes_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = FakeResponse()
    monkeypatch.setattr(
        downloader_module.tempfile,
        "NamedTemporaryFile",
        lambda **kwargs: (_ for _ in ()).throw(OSError("private filesystem detail")),
    )
    with pytest.raises(DownloadError, match="Could not create temporary") as caught:
        ArtifactDownloader(FakeSession(response)).download(make_asset(), make_target(tmp_path))
    assert caught.value.retryable is False
    assert response.closed
    assert not temporary_downloads(tmp_path)


@pytest.mark.parametrize("algorithm", ["sha256", "sha1", "md5"])
def test_supported_checksum_verification_succeeds(tmp_path: Path, algorithm: str) -> None:
    result = ArtifactDownloader(FakeSession(FakeResponse())).download(
        make_asset(algorithm=algorithm), make_target(tmp_path)
    )
    assert result.checksum_algorithm == algorithm
    assert result.checksum == hashlib.new(algorithm, CONTENT).hexdigest()


def test_missing_content_length_is_accepted(tmp_path: Path) -> None:
    result = ArtifactDownloader(FakeSession(FakeResponse(headers={}))).download(
        make_asset(), make_target(tmp_path)
    )
    assert result.bytes_written == len(CONTENT)


def test_valid_content_length_is_enforced(tmp_path: Path) -> None:
    result = ArtifactDownloader(
        FakeSession(FakeResponse(headers={"Content-Length": str(len(CONTENT))}))
    ).download(make_asset(), make_target(tmp_path))
    assert result.bytes_written == len(CONTENT)


@pytest.mark.parametrize("value", ["invalid", "-1", "1.5", True])
def test_invalid_content_length_is_rejected(tmp_path: Path, value: object) -> None:
    response = FakeResponse(headers={"Content-Length": value})
    with pytest.raises(DownloadError, match="invalid Content-Length"):
        ArtifactDownloader(FakeSession(response)).download(make_asset(), make_target(tmp_path))
    assert response.closed


def test_mismatched_content_length_removes_temp_and_preserves_final(tmp_path: Path) -> None:
    target = make_target(tmp_path)
    final = tmp_path / "application-1.0.0.jar"
    final.write_bytes(b"existing")
    response = FakeResponse(headers={"Content-Length": str(len(CONTENT) + 1)})
    with pytest.raises(DownloadError, match="does not match"):
        ArtifactDownloader(FakeSession(response)).download(make_asset(), target)
    assert final.read_bytes() == b"existing"
    assert not temporary_downloads(tmp_path)
    assert response.closed


def test_checksum_mismatch_preserves_final_and_removes_temp(tmp_path: Path) -> None:
    target = make_target(tmp_path)
    final = tmp_path / "application-1.0.0.jar"
    final.write_bytes(b"existing")
    response = FakeResponse()
    with pytest.raises(DownloadError, match="sha256 checksum does not match"):
        ArtifactDownloader(FakeSession(response)).download(
            make_asset(checksum="0" * 64), target
        )
    assert final.read_bytes() == b"existing"
    assert not temporary_downloads(tmp_path)
    assert response.closed


@pytest.mark.parametrize(
    ("error", "message"),
    [(requests.Timeout(), "timed out"), (requests.ConnectionError(), "Could not connect")],
)
def test_request_failures_are_translated(
    tmp_path: Path, error: Exception, message: str
) -> None:
    with pytest.raises(DownloadError, match=message):
        ArtifactDownloader(FakeSession(error)).download(make_asset(), make_target(tmp_path))


@pytest.mark.parametrize(
    ("status", "message"),
    [(401, "authentication failed"), (403, "permission denied"), (503, "HTTP 503")],
)
def test_http_errors_are_translated_and_response_closed(
    tmp_path: Path, status: int, message: str
) -> None:
    response = FakeResponse(status_code=status)
    with pytest.raises(DownloadError, match=message):
        ArtifactDownloader(FakeSession(response)).download(make_asset(), make_target(tmp_path))
    assert response.closed


def test_stream_failure_preserves_final_and_removes_temp(tmp_path: Path) -> None:
    final = tmp_path / "application-1.0.0.jar"
    final.write_bytes(b"existing")
    response = FakeResponse(
        [b"partial"], stream_error=requests.ConnectionError("server secret")
    )
    with pytest.raises(DownloadError, match="stream failed"):
        ArtifactDownloader(FakeSession(response)).download(make_asset(), make_target(tmp_path))
    assert final.read_bytes() == b"existing"
    assert not temporary_downloads(tmp_path)
    assert response.closed


def test_programming_stream_error_propagates_with_cleanup(tmp_path: Path) -> None:
    final = tmp_path / "application-1.0.0.jar"
    final.write_bytes(b"existing")
    response = FakeResponse([b"partial"], stream_error=RuntimeError("programming defect"))
    with pytest.raises(RuntimeError, match="programming defect"):
        ArtifactDownloader(FakeSession(response)).download(make_asset(), make_target(tmp_path))
    assert final.read_bytes() == b"existing"
    assert not temporary_downloads(tmp_path)
    assert response.closed


def test_write_failure_preserves_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    final = tmp_path / "application-1.0.0.jar"
    final.write_bytes(b"existing")
    original = downloader_module.tempfile.NamedTemporaryFile

    class FailingWriter:
        def __init__(self, wrapped: Any) -> None:
            self.wrapped = wrapped
            self.name = wrapped.name

        def __enter__(self) -> "FailingWriter":
            return self

        def __exit__(self, *args: object) -> None:
            self.wrapped.close()

        def write(self, chunk: bytes) -> None:
            raise OSError("simulated write failure")

        def __getattr__(self, name: str) -> Any:
            return getattr(self.wrapped, name)

    monkeypatch.setattr(
        downloader_module.tempfile,
        "NamedTemporaryFile",
        lambda **kwargs: FailingWriter(original(**kwargs)),
    )
    with pytest.raises(DownloadError, match="Could not write"):
        ArtifactDownloader(FakeSession(FakeResponse())).download(
            make_asset(), make_target(tmp_path)
        )
    assert final.read_bytes() == b"existing"
    assert not temporary_downloads(tmp_path)


def test_fsync_failure_preserves_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    final = tmp_path / "application-1.0.0.jar"
    final.write_bytes(b"existing")
    monkeypatch.setattr(downloader_module.os, "fsync", lambda descriptor: (_ for _ in ()).throw(OSError()))
    with pytest.raises(DownloadError, match="Could not flush") as caught:
        ArtifactDownloader(FakeSession(FakeResponse())).download(
            make_asset(), make_target(tmp_path)
        )
    assert caught.value.retryable is False
    assert final.read_bytes() == b"existing"
    assert not temporary_downloads(tmp_path)


def test_atomic_replace_failure_preserves_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    final = tmp_path / "application-1.0.0.jar"
    final.write_bytes(b"existing")
    monkeypatch.setattr(
        downloader_module.os,
        "replace",
        lambda source, destination: (_ for _ in ()).throw(OSError()),
    )
    with pytest.raises(DownloadError, match="atomically deploy"):
        ArtifactDownloader(FakeSession(FakeResponse())).download(
            make_asset(), make_target(tmp_path)
        )
    assert final.read_bytes() == b"existing"
    assert not temporary_downloads(tmp_path)


def test_success_atomically_replaces_existing_file(tmp_path: Path) -> None:
    final = tmp_path / "application-1.0.0.jar"
    final.write_bytes(b"existing")
    ArtifactDownloader(FakeSession(FakeResponse())).download(make_asset(), make_target(tmp_path))
    assert final.read_bytes() == CONTENT


@pytest.mark.parametrize(
    "filename",
    ["", ".", "..", "../escape.jar", "..\\escape.jar", "/absolute.jar", "C:\\absolute.jar"],
)
def test_unsafe_filenames_fail_before_side_effects(tmp_path: Path, filename: str) -> None:
    destination = tmp_path / "destination"
    session = FakeSession(FakeResponse())
    with pytest.raises(DownloadError, match="Unsafe"):
        ArtifactDownloader(session).download(make_asset(filename=filename), make_target(destination))
    assert not session.calls
    assert not destination.exists()


@pytest.mark.parametrize(
    "filename",
    ["other-1.0.0.jar", "application-2.0.0.jar", "application-1.0.0-sources.jar", "application-1.0.0.pom"],
)
def test_inconsistent_filename_fails_before_side_effects(tmp_path: Path, filename: str) -> None:
    destination = tmp_path / "destination"
    session = FakeSession(FakeResponse())
    with pytest.raises(DownloadError, match="does not match"):
        ArtifactDownloader(session).download(make_asset(filename=filename), make_target(destination))
    assert not session.calls
    assert not destination.exists()


def test_classified_filename_must_match_exactly(tmp_path: Path) -> None:
    asset = make_asset(filename="application-1.0.0-all.jar")
    result = ArtifactDownloader(FakeSession(FakeResponse())).download(
        asset, make_target(tmp_path, classifier="all")
    )
    assert result.path.name == "application-1.0.0-all.jar"


def test_credentials_do_not_appear_in_errors_or_repr(tmp_path: Path) -> None:
    username = "private-user"
    password = "private-password"
    downloader = ArtifactDownloader(FakeSession(requests.ConnectionError(f"{username}:{password}")))
    with pytest.raises(DownloadError) as error:
        downloader.download(
            make_asset(), make_target(tmp_path, username=username, password=password)
        )
    combined = f"{error.value!r} {downloader!r}"
    assert username not in combined
    assert password not in combined


def test_download_does_not_create_state(tmp_path: Path) -> None:
    destination = tmp_path / "destination"
    state_directory = tmp_path / "state"
    ArtifactDownloader(FakeSession(FakeResponse())).download(
        make_asset(), make_target(destination)
    )
    assert not state_directory.exists()


@pytest.mark.parametrize("error", [requests.Timeout(), requests.ConnectionError()])
def test_transient_request_failures_are_retryable(tmp_path: Path, error: Exception) -> None:
    with pytest.raises(DownloadError) as caught:
        ArtifactDownloader(FakeSession(error)).download(make_asset(), make_target(tmp_path))
    assert caught.value.retryable is True


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(408, True), (429, True), (500, True), (503, True), (401, False), (403, False), (400, False), (404, False)],
)
def test_http_retry_classification(tmp_path: Path, status: int, retryable: bool) -> None:
    with pytest.raises(DownloadError) as caught:
        ArtifactDownloader(FakeSession(FakeResponse(status_code=status))).download(
            make_asset(), make_target(tmp_path)
        )
    assert caught.value.retryable is retryable


@pytest.mark.parametrize("failure", ["stream", "length", "checksum"])
def test_transfer_integrity_failures_are_retryable(tmp_path: Path, failure: str) -> None:
    if failure == "stream":
        response = FakeResponse(stream_error=requests.exceptions.ChunkedEncodingError())
        asset = make_asset()
    elif failure == "length":
        response = FakeResponse(headers={"Content-Length": str(len(CONTENT) + 1)})
        asset = make_asset()
    else:
        response = FakeResponse()
        asset = make_asset(checksum="0" * 64)
    with pytest.raises(DownloadError) as caught:
        ArtifactDownloader(FakeSession(response)).download(asset, make_target(tmp_path))
    assert caught.value.retryable is True


def test_unsafe_filename_failure_is_not_retryable(tmp_path: Path) -> None:
    with pytest.raises(DownloadError) as caught:
        ArtifactDownloader(FakeSession(FakeResponse())).download(
            make_asset(filename="../escape.jar"), make_target(tmp_path)
        )
    assert caught.value.retryable is False


def test_atomic_replacement_failure_is_not_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        downloader_module.os,
        "replace",
        lambda source, destination: (_ for _ in ()).throw(OSError()),
    )
    with pytest.raises(DownloadError) as caught:
        ArtifactDownloader(FakeSession(FakeResponse())).download(make_asset(), make_target(tmp_path))
    assert caught.value.retryable is False
