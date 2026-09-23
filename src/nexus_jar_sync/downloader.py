"""Safe streaming download and atomic deployment of a discovered Nexus asset."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import os
from pathlib import Path
import tempfile
from typing import Any, Protocol

import requests

from nexus_jar_sync.config import TargetConfig
from nexus_jar_sync.nexus_client import NexusAsset, NexusClientError


class DownloadError(Exception):
    """Raised when an artifact cannot be safely downloaded and deployed."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    filename: str
    bytes_written: int
    checksum_algorithm: str
    checksum: str


class _Session(Protocol):
    def get(self, url: str, **kwargs: Any) -> Any: ...


class ArtifactDownloader:
    """Download one preselected asset without discovery, retries, or state writes."""

    CHUNK_SIZE = 1024 * 1024

    def __init__(self, session: _Session | None = None) -> None:
        self._session = session if session is not None else requests.Session()

    def download(self, asset: NexusAsset, target: TargetConfig) -> DownloadResult:
        final_path = self._validated_final_path(asset, target)
        try:
            checksum_algorithm, expected_checksum = asset.canonical_checksum
            digest = hashlib.new(checksum_algorithm)
        except (NexusClientError, ValueError):
            raise DownloadError(
                f"Asset has no supported checksum for target '{target.id}'"
            ) from None

        try:
            target.destination.directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise DownloadError(
                f"Could not create destination directory for target '{target.id}'"
            ) from None

        auth = None
        if target.auth.username is not None and target.auth.password is not None:
            auth = (target.auth.username, target.auth.password)
        verify: bool | str = target.network.verify_tls
        if target.network.ca_bundle is not None:
            verify = str(target.network.ca_bundle)

        try:
            response = self._session.get(
                asset.download_url,
                auth=auth,
                timeout=target.network.timeout_seconds,
                verify=verify,
                stream=True,
            )
        except requests.Timeout:
            raise DownloadError(
                f"Download timed out for target '{target.id}'", retryable=True
            ) from None
        except requests.ConnectionError:
            raise DownloadError(
                f"Could not connect while downloading target '{target.id}'", retryable=True
            ) from None
        except requests.RequestException:
            raise DownloadError(f"Download request failed for target '{target.id}'") from None

        temporary_path: Path | None = None
        try:
            self._validate_response(response, target)
            content_length = self._content_length(response, target)
            try:
                temporary_file = tempfile.NamedTemporaryFile(
                    mode="w+b",
                    dir=target.destination.directory,
                    prefix=f".{asset.filename}.",
                    suffix=".download.tmp",
                    delete=False,
                )
                temporary_path = Path(temporary_file.name)
            except OSError:
                raise DownloadError(
                    f"Could not create temporary download file for target '{target.id}'"
                ) from None

            bytes_written = 0
            try:
                with temporary_file:
                    try:
                        chunks = iter(response.iter_content(chunk_size=self.CHUNK_SIZE))
                    except Exception:
                        raise DownloadError(
                            f"Download stream failed for target '{target.id}'", retryable=True
                        ) from None
                    while True:
                        try:
                            chunk = next(chunks)
                        except StopIteration:
                            break
                        except Exception:
                            raise DownloadError(
                                f"Download stream failed for target '{target.id}'", retryable=True
                            ) from None
                        if not chunk:
                            continue
                        if not isinstance(chunk, bytes):
                            raise DownloadError(
                                f"Download stream returned invalid data for target '{target.id}'"
                            )
                        try:
                            temporary_file.write(chunk)
                        except (OSError, TypeError):
                            raise DownloadError(
                                f"Could not write temporary download for target '{target.id}'"
                            ) from None
                        digest.update(chunk)
                        bytes_written += len(chunk)
                    try:
                        temporary_file.flush()
                        os.fsync(temporary_file.fileno())
                    except OSError:
                        raise DownloadError(
                            f"Could not flush temporary download for target '{target.id}'"
                        ) from None
            except DownloadError:
                raise
            except OSError:
                raise DownloadError(
                    f"Could not close temporary download for target '{target.id}'"
                ) from None

            if content_length is not None and bytes_written != content_length:
                raise DownloadError(
                    f"Download size does not match Content-Length for target '{target.id}'",
                    retryable=True,
                )
            actual_checksum = digest.hexdigest()
            if not hmac.compare_digest(actual_checksum, expected_checksum):
                raise DownloadError(
                    f"Downloaded {checksum_algorithm} checksum does not match for target '{target.id}'",
                    retryable=True,
                )
            try:
                os.replace(temporary_path, final_path)
            except OSError:
                raise DownloadError(
                    f"Could not atomically deploy artifact for target '{target.id}'"
                ) from None
            temporary_path = None
            return DownloadResult(
                path=final_path,
                filename=asset.filename,
                bytes_written=bytes_written,
                checksum_algorithm=checksum_algorithm,
                checksum=actual_checksum,
            )
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                response.close()
            except Exception:
                pass

    @staticmethod
    def _validated_final_path(asset: NexusAsset, target: TargetConfig) -> Path:
        filename = asset.filename
        if (
            not isinstance(filename, str)
            or not filename
            or filename in {".", ".."}
            or "/" in filename
            or "\\" in filename
            or Path(filename).is_absolute()
            or Path(filename).name != filename
        ):
            raise DownloadError(f"Unsafe artifact filename for target '{target.id}'")
        classifier_suffix = (
            f"-{target.artifact.classifier}" if target.artifact.classifier is not None else ""
        )
        expected = (
            f"{target.nexus.artifact_id}-{asset.version}{classifier_suffix}."
            f"{target.artifact.extension}"
        )
        if filename != expected:
            raise DownloadError(
                f"Artifact filename does not match target '{target.id}' coordinates"
            )
        destination = target.destination.directory.resolve(strict=False)
        final_path = destination / filename
        if final_path.resolve(strict=False).parent != destination:
            raise DownloadError(f"Unsafe artifact filename for target '{target.id}'")
        return final_path

    @staticmethod
    def _validate_response(response: Any, target: TargetConfig) -> None:
        status_code = getattr(response, "status_code", None)
        if status_code == 401:
            raise DownloadError(f"Download authentication failed for target '{target.id}'")
        if status_code == 403:
            raise DownloadError(f"Download permission denied for target '{target.id}'")
        if not isinstance(status_code, int) or not 200 <= status_code < 300:
            status = status_code if isinstance(status_code, int) else "unknown"
            raise DownloadError(
                f"Download returned HTTP {status} for target '{target.id}'",
                retryable=isinstance(status_code, int)
                and (status_code in {408, 429} or 500 <= status_code < 600),
            )

    @staticmethod
    def _content_length(response: Any, target: TargetConfig) -> int | None:
        headers = getattr(response, "headers", {})
        try:
            value = headers.get("Content-Length")
        except AttributeError:
            raise DownloadError(
                f"Download returned malformed headers for target '{target.id}'"
            ) from None
        if value is None:
            return None
        if isinstance(value, bool):
            raise DownloadError(
                f"Download returned invalid Content-Length for target '{target.id}'"
            )
        try:
            length = int(value)
        except (TypeError, ValueError):
            raise DownloadError(
                f"Download returned invalid Content-Length for target '{target.id}'"
            ) from None
        if length < 0:
            raise DownloadError(
                f"Download returned invalid Content-Length for target '{target.id}'"
            )
        return length
