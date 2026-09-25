"""Assemble and publish immutable primary-plus-companion release directories."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
from typing import Any, Callable, Protocol

import requests

from nexus_jar_sync.config import CompanionConfig, TargetConfig, ToolsConfig
from nexus_jar_sync.downloader import ArtifactDownloader, DownloadDisposition, DownloadError, DownloadResult
from nexus_jar_sync.nexus_client import NexusAsset


METADATA_NAME = ".nexus-jar-sync-release.json"


class _Session(Protocol):
    def get(self, url: str, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class CompanionRecord:
    id: str
    filename: str
    action: str
    sha256: str
    size: int
    archive_retained: bool
    extract_to: str | None


class ReleaseAssembler:
    """Build a release privately and publish its version directory once."""

    def __init__(
        self,
        primary_downloader: ArtifactDownloader,
        session: _Session | None = None,
        *,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._primary = primary_downloader
        self._owns_session = session is None
        self._session = session if session is not None else requests.Session()
        self._run = command_runner
        self._clock = clock

    def close(self) -> None:
        self._primary.close()
        if self._owns_session:
            self._session.close()

    def assemble(
        self,
        asset: NexusAsset,
        target: TargetConfig,
        tools: ToolsConfig,
        *,
        destination_base: Path | None = None,
    ) -> DownloadResult:
        base = (destination_base or target.destination.directory).resolve(strict=False)
        final_primary = ArtifactDownloader._validated_final_path(
            asset, replace(target, destination=replace(target.destination, directory=base))
        )
        final_release = final_primary.parent
        existing = self._validate_completed_release(final_release, asset, target)
        if existing is not None:
            return existing
        if final_release.exists() or final_release.is_symlink():
            raise DownloadError(f"Incomplete or conflicting release for target '{target.id}'")

        try:
            base.mkdir(parents=True, exist_ok=True)
            if _unsafe_node(base) or not base.is_dir():
                raise OSError
            stage_base = Path(tempfile.mkdtemp(prefix=f".{target.id}-release-", dir=base))
        except OSError:
            raise DownloadError(f"Could not create release staging area for target '{target.id}'") from None

        try:
            staging_target = replace(
                target, destination=replace(target.destination, directory=stage_base)
            )
            primary = self._primary.download(asset, staging_target)
            release_root = primary.path.parent
            records: list[CompanionRecord] = []
            for companion in target.companions:
                archive, digest, size = self._download_companion(companion, target, release_root)
                if companion.action == "extract_7z":
                    self._extract(companion, archive, release_root, tools, target)
                    if not companion.keep_archive:
                        archive.unlink()
                records.append(
                    CompanionRecord(
                        companion.id,
                        companion.filename,
                        companion.action,
                        digest,
                        size,
                        companion.keep_archive,
                        companion.extract_to.as_posix() if companion.action == "extract_7z" else None,
                    )
                )
            metadata = {
                "target_id": target.id,
                "primary_version": asset.version,
                "primary_filename": asset.filename,
                "primary_checksum_algorithm": primary.checksum_algorithm,
                "primary_checksum": primary.checksum,
                "companions": [asdict(record) for record in records],
                "completed_at": self._aware_timestamp().isoformat(),
            }
            self._write_metadata(release_root / METADATA_NAME, metadata, target)
            try:
                final_release.mkdir()
            except FileExistsError:
                winner = self._validate_completed_release(final_release, asset, target)
                if winner is None:
                    raise DownloadError(f"Release publication conflict for target '{target.id}'")
                return winner
            except OSError:
                winner = self._validate_completed_release(final_release, asset, target)
                if winner is not None:
                    return winner
                raise DownloadError(f"Could not publish release for target '{target.id}'") from None
            try:
                # Directory replacement is not consistently no-clobber. Reserve a new
                # directory, move staged entries into it, and publish metadata last.
                entries = sorted(
                    (path for path in release_root.iterdir() if path.name != METADATA_NAME),
                    key=lambda path: path.name,
                )
                for entry in entries:
                    os.rename(entry, final_release / entry.name)
                os.rename(release_root / METADATA_NAME, final_release / METADATA_NAME)
            except OSError:
                raise DownloadError(f"Could not publish release for target '{target.id}'") from None
            return replace(primary, path=final_release / asset.filename)
        finally:
            shutil.rmtree(stage_base, ignore_errors=True)

    def _download_companion(
        self, companion: CompanionConfig, target: TargetConfig, release_root: Path
    ) -> tuple[Path, str, int]:
        output = release_root / companion.filename
        if output.exists() or output.is_symlink() or output.parent != release_root:
            raise DownloadError(f"Companion collision for target '{target.id}'")
        auth_config = companion.auth or target.auth
        auth = None
        if auth_config.username is not None and auth_config.password is not None:
            auth = (auth_config.username, auth_config.password)
        verify: bool | str = target.network.verify_tls
        if target.network.ca_bundle is not None:
            verify = str(target.network.ca_bundle)
        try:
            response = self._session.get(
                companion.url,
                auth=auth,
                timeout=target.network.timeout_seconds,
                verify=verify,
                stream=True,
                allow_redirects=False,
            )
        except requests.Timeout:
            raise DownloadError(f"Companion download timed out for target '{target.id}'", retryable=True) from None
        except requests.ConnectionError:
            raise DownloadError(f"Could not connect while downloading companion for target '{target.id}'", retryable=True) from None
        except requests.RequestException:
            raise DownloadError(f"Companion download failed for target '{target.id}'") from None
        try:
            status = getattr(response, "status_code", None)
            if isinstance(status, int) and 300 <= status < 400:
                raise DownloadError(f"Companion redirect rejected for target '{target.id}'")
            ArtifactDownloader._validate_response(response, target)
            length = ArtifactDownloader._content_length(response, target)
            digest = hashlib.sha256()
            size = 0
            try:
                with output.open("xb") as stream:
                    for chunk in response.iter_content(chunk_size=ArtifactDownloader.CHUNK_SIZE):
                        if not chunk:
                            continue
                        if not isinstance(chunk, bytes):
                            raise DownloadError(f"Companion stream returned invalid data for target '{target.id}'")
                        stream.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
            except DownloadError:
                raise
            except requests.RequestException:
                raise DownloadError(f"Companion stream failed for target '{target.id}'", retryable=True) from None
            except OSError:
                raise DownloadError(f"Could not store companion for target '{target.id}'") from None
            if length is not None and length != size:
                raise DownloadError(f"Companion size mismatch for target '{target.id}'", retryable=True)
            return output, digest.hexdigest(), size
        finally:
            try:
                response.close()
            except Exception:
                pass

    def _extract(
        self,
        companion: CompanionConfig,
        archive: Path,
        release_root: Path,
        tools: ToolsConfig,
        target: TargetConfig,
    ) -> None:
        executable = tools.seven_zip_executable
        if executable is None:
            raise DownloadError(f"7-Zip is not configured for target '{target.id}'")
        listing = self._execute_7z(
            [str(executable), "l", "-slt", str(archive)], tools, target
        )
        for line in listing.stdout.splitlines():
            if line.startswith("Path = "):
                entry = line[7:]
                if entry != archive.name and Path(entry).name != archive.name:
                    _validate_archive_path(entry, target.id)
            if line.startswith(("Symbolic Link =", "Hard Link =")):
                raise DownloadError(f"Unsafe archive link for target '{target.id}'")
            if line.startswith("Attributes =") and "L" in line.split("=", 1)[1].upper():
                raise DownloadError(f"Unsafe archive link for target '{target.id}'")
        scratch = Path(tempfile.mkdtemp(prefix=".extract-", dir=release_root.parent))
        try:
            self._execute_7z(
                [str(executable), "x", str(archive), f"-o{scratch}", "-y"], tools, target
            )
            _validate_tree(scratch, target.id)
            destination = release_root if companion.extract_to == Path(".") else release_root / companion.extract_to
            try:
                destination.mkdir(parents=True, exist_ok=True)
            except OSError:
                raise DownloadError(f"Extraction collision for target '{target.id}'") from None
            for source in sorted(scratch.rglob("*"), key=lambda path: (len(path.parts), path.as_posix())):
                relative = source.relative_to(scratch)
                target_path = destination / relative
                if target_path.resolve(strict=False).is_relative_to(release_root.resolve(strict=True)) is False:
                    raise DownloadError(f"Unsafe extracted path for target '{target.id}'")
                if source.is_dir():
                    if target_path.exists() and not target_path.is_dir():
                        raise DownloadError(f"Extraction collision for target '{target.id}'")
                    target_path.mkdir(exist_ok=True)
                else:
                    if target_path.exists() or target_path.is_symlink():
                        raise DownloadError(f"Extraction collision for target '{target.id}'")
                    os.rename(source, target_path)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def _execute_7z(
        self, command: list[str], tools: ToolsConfig, target: TargetConfig
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = self._run(
                command,
                shell=False,
                capture_output=True,
                text=True,
                timeout=tools.extraction_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise DownloadError(f"7-Zip timed out for target '{target.id}'") from None
        except OSError:
            raise DownloadError(f"7-Zip could not be executed for target '{target.id}'") from None
        if result.returncode != 0:
            raise DownloadError(f"7-Zip failed for target '{target.id}'")
        if len(result.stdout) > 4 * 1024 * 1024 or len(result.stderr) > 1024 * 1024:
            raise DownloadError(f"7-Zip output exceeded the safety limit for target '{target.id}'")
        return result

    @staticmethod
    def _write_metadata(path: Path, value: dict[str, Any], target: TargetConfig) -> None:
        try:
            with path.open("x", encoding="utf-8") as output:
                json.dump(value, output, indent=2, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
        except OSError:
            raise DownloadError(f"Could not write release metadata for target '{target.id}'") from None

    def _validate_completed_release(
        self, release: Path, asset: NexusAsset, target: TargetConfig
    ) -> DownloadResult | None:
        metadata_path = release / METADATA_NAME
        primary_path = release / asset.filename
        if not metadata_path.is_file():
            return None
        if _unsafe_node(release) or _unsafe_node(metadata_path) or _unsafe_node(primary_path):
            raise DownloadError(f"Unsafe completed release for target '{target.id}'")
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            algorithm, expected = asset.canonical_checksum
            if (
                metadata.get("target_id") != target.id
                or metadata.get("primary_version") != asset.version
                or metadata.get("primary_filename") != asset.filename
                or not primary_path.is_file()
            ):
                return None
            if (
                metadata.get("primary_checksum_algorithm") != algorithm
                or metadata.get("primary_checksum") != expected
            ):
                raise DownloadError(f"Artifact checksum conflict for target '{target.id}'")
            actual = _hash_file(primary_path, algorithm)
            if actual != expected:
                raise DownloadError(f"Artifact checksum conflict for target '{target.id}'")
            for item in metadata.get("companions", []):
                if item.get("archive_retained"):
                    path = release / item["filename"]
                    if _unsafe_node(path) or not path.is_file() or path.stat().st_size != item.get("size") or _hash_file(path, "sha256") != item.get("sha256"):
                        return None
            return DownloadResult(primary_path, asset.filename, primary_path.stat().st_size, algorithm, actual, DownloadDisposition.REUSED)
        except DownloadError:
            raise
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None

    def _aware_timestamp(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("release clock must be timezone-aware")
        return value


def _hash_file(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _unsafe_node(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        details = path.lstat()
        return bool(getattr(details, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    except OSError:
        return True


def _validate_archive_path(value: str, target_id: str) -> None:
    from nexus_jar_sync.config import _safe_relative_path

    if not _safe_relative_path(value, allow_subdirectories=True) or value == ".":
        raise DownloadError(f"Unsafe archive entry for target '{target_id}'")


def _validate_tree(root: Path, target_id: str) -> None:
    for path in root.rglob("*"):
        if _unsafe_node(path):
            raise DownloadError(f"Unsafe extracted filesystem entry for target '{target_id}'")
        try:
            if path.is_file() and path.stat().st_nlink > 1:
                raise DownloadError(f"Unsafe extracted hard link for target '{target_id}'")
        except OSError:
            raise DownloadError(f"Unsafe extracted filesystem entry for target '{target_id}'") from None
        _validate_archive_path(path.relative_to(root).as_posix(), target_id)
