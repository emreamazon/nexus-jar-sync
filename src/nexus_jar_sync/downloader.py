"""Safe streaming download and atomic deployment of a discovered Nexus asset."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import hmac
import os
from pathlib import Path
import re
import stat
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


class DownloadDisposition(Enum):
    DOWNLOADED = "downloaded"
    REUSED = "reused"


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    filename: str
    bytes_written: int
    checksum_algorithm: str
    checksum: str
    disposition: DownloadDisposition = DownloadDisposition.DOWNLOADED


class _Session(Protocol):
    def get(self, url: str, **kwargs: Any) -> Any: ...


_WINDOWS_INVALID = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CLOCK$"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)


def _safe_component(value: object, description: str, target_id: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or value[-1] in {" ", "."}
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or any(character in _WINDOWS_INVALID for character in value)
        or Path(value).is_absolute()
        or re.match(r"^[A-Za-z]:", value) is not None
        or value.split(".", 1)[0].upper() in _WINDOWS_RESERVED
    ):
        raise DownloadError(f"Unsafe {description} for target '{target_id}'")
    return value


def _unsafe_existing_path(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        details = path.lstat()
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        return bool(getattr(details, "st_file_attributes", 0) & reparse_flag)
    except OSError:
        return True


class ArtifactDownloader:
    """Download one preselected asset without discovery, retries, or state writes."""

    CHUNK_SIZE = 1024 * 1024

    def __init__(self, session: _Session | None = None) -> None:
        self._owns_session = session is None
        self._session = session if session is not None else requests.Session()

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    def download(self, asset: NexusAsset, target: TargetConfig) -> DownloadResult:
        final_path = self._validated_final_path(asset, target)
        try:
            checksum_algorithm, expected_checksum = asset.canonical_checksum
            digest = hashlib.new(checksum_algorithm)
        except (NexusClientError, ValueError):
            raise DownloadError(
                f"Asset has no supported checksum for target '{target.id}'"
            ) from None

        version_directory = final_path.parent
        self._create_safe_version_directory(version_directory, target)
        existing = self._existing_result(final_path, asset, target)
        if existing is not None:
            return existing

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
                    dir=version_directory,
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
                    except requests.RequestException:
                        raise DownloadError(
                            f"Download stream failed for target '{target.id}'", retryable=True
                        ) from None
                    while True:
                        try:
                            chunk = next(chunks)
                        except StopIteration:
                            break
                        except requests.RequestException:
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
                os.link(temporary_path, final_path)
            except FileExistsError:
                winner = self._existing_result(final_path, asset, target)
                if winner is None:
                    raise DownloadError(f"Artifact publication conflict for target '{target.id}'")
                return winner
            except OSError:
                raise DownloadError(
                    f"Could not publish artifact without overwriting for target '{target.id}'"
                ) from None
            try:
                temporary_path.unlink()
            except OSError:
                raise DownloadError(
                    f"Could not remove owned temporary file for target '{target.id}'"
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
        version = _safe_component(asset.version, "artifact version", target.id)
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
        _safe_component(filename, "artifact filename", target.id)
        destination = target.destination.directory.resolve(strict=False)
        version_directory = destination / version
        final_path = version_directory / filename
        if version_directory.parent != destination or final_path.parent != version_directory:
            raise DownloadError(f"Unsafe artifact filename for target '{target.id}'")
        return final_path

    def final_path(self, asset: NexusAsset, target: TargetConfig) -> Path:
        """Return the validated versioned deployment path without filesystem changes."""
        return self._validated_final_path(asset, target)

    @staticmethod
    def _create_safe_version_directory(version_directory: Path, target: TargetConfig) -> None:
        base = version_directory.parent
        try:
            base.mkdir(parents=True, exist_ok=True)
            if _unsafe_existing_path(base) or not base.is_dir():
                raise OSError
            version_directory.mkdir(exist_ok=True)
            if _unsafe_existing_path(version_directory) or not version_directory.is_dir():
                raise OSError
            if version_directory.resolve(strict=True).parent != base.resolve(strict=True):
                raise OSError
        except OSError:
            raise DownloadError(
                f"Could not create destination/version directory for target '{target.id}'"
            ) from None

    @staticmethod
    def _existing_result(
        final_path: Path, asset: NexusAsset, target: TargetConfig
    ) -> DownloadResult | None:
        try:
            exists = final_path.exists() or final_path.is_symlink()
        except OSError:
            exists = True
        if not exists:
            return None
        if _unsafe_existing_path(final_path) or not final_path.is_file():
            raise DownloadError(f"Unsafe existing artifact path for target '{target.id}'")
        algorithm, expected = asset.canonical_checksum
        try:
            digest = hashlib.new(algorithm)
            size = 0
            with final_path.open("rb") as artifact_file:
                for chunk in iter(lambda: artifact_file.read(ArtifactDownloader.CHUNK_SIZE), b""):
                    digest.update(chunk)
                    size += len(chunk)
        except OSError:
            raise DownloadError(f"Could not verify existing artifact for target '{target.id}'") from None
        actual = digest.hexdigest()
        if not hmac.compare_digest(actual, expected):
            raise DownloadError(f"Artifact checksum conflict for target '{target.id}'")
        return DownloadResult(
            path=final_path,
            filename=asset.filename,
            bytes_written=size,
            checksum_algorithm=algorithm,
            checksum=actual,
            disposition=DownloadDisposition.REUSED,
        )

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
