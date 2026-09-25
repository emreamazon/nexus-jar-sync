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
import sys
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
    strip_single_root: bool


@dataclass(frozen=True)
class ArtifactRecord:
    id: str
    artifact_id: str
    role: str
    filename: str
    checksum_algorithm: str
    checksum: str
    size: int
    path: str


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
        secondary_resolver: Callable[[str], NexusAsset] | None = None,
    ) -> DownloadResult:
        base = (destination_base or target.destination.directory).resolve(strict=False)
        final_primary = ArtifactDownloader._validated_final_path(
            asset, replace(target, destination=replace(target.destination, directory=base))
        )
        final_release = final_primary.parent
        _validate_final_path_compatibility(final_primary, target.id)
        existing = self._validate_completed_release(final_release, asset, target)
        if existing is not None:
            return existing
        if final_release.exists() or final_release.is_symlink():
            raise DownloadError(f"Incomplete or conflicting release for target '{target.id}'")

        try:
            base.mkdir(parents=True, exist_ok=True)
            if _unsafe_node(base) or not base.is_dir():
                raise OSError
            stage_base = Path(tempfile.mkdtemp(prefix=".njs-", dir=base))
        except OSError:
            raise DownloadError(f"Could not create release staging area for target '{target.id}'") from None

        try:
            staging_target = replace(
                target, destination=replace(target.destination, directory=stage_base)
            )
            primary = self._primary.download(asset, staging_target)
            release_root = primary.path.parent
            artifact_records = [
                ArtifactRecord(
                    "primary", target.nexus.artifact_id, "primary", asset.filename,
                    primary.checksum_algorithm, primary.checksum, primary.bytes_written,
                    asset.filename,
                )
            ]
            roles = {asset.filename: "primary"}
            for configured in target.release_artifacts:
                if secondary_resolver is None:
                    raise DownloadError(f"Secondary artifact resolver is unavailable for target '{target.id}'")
                secondary_asset = secondary_resolver(configured.artifact_id)
                if secondary_asset.version != asset.version:
                    raise DownloadError(f"Secondary artifact version mismatch for target '{target.id}'")
                if secondary_asset.filename in roles:
                    raise DownloadError(f"Release artifact filename collision for target '{target.id}'")
                secondary_target = replace(
                    staging_target,
                    nexus=replace(staging_target.nexus, artifact_id=configured.artifact_id),
                    release_artifacts=(),
                )
                secondary = self._primary.download(secondary_asset, secondary_target)
                roles[secondary_asset.filename] = "secondary"
                artifact_records.append(
                    ArtifactRecord(
                        configured.id, configured.artifact_id, "secondary",
                        secondary_asset.filename, secondary.checksum_algorithm,
                        secondary.checksum, secondary.bytes_written, secondary_asset.filename,
                    )
                )
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
                        companion.strip_single_root,
                    )
                )
            metadata = {
                "schema_version": 1,
                "target_id": target.id,
                "primary_version": asset.version,
                "primary_filename": asset.filename,
                "primary_checksum_algorithm": primary.checksum_algorithm,
                "primary_checksum": primary.checksum,
                "artifacts": [asdict(record) for record in artifact_records],
                "companions": [asdict(record) for record in records],
                "completed_at": self._aware_timestamp().isoformat(),
            }
            roles.update(
                {
                    companion.filename: "companion"
                    for companion in target.companions
                    if companion.action == "copy" or companion.keep_archive
                }
            )
            metadata["files"] = _inventory_release(release_root, roles, target.id)
            self._write_metadata(release_root / METADATA_NAME, metadata, target)
            winner = self._publish_release(release_root, final_release, asset, target)
            return winner or replace(primary, path=final_release / asset.filename)
        finally:
            shutil.rmtree(stage_base, ignore_errors=True)

    def _publish_release(
        self, release_root: Path, final_release: Path, asset: NexusAsset, target: TargetConfig
    ) -> DownloadResult | None:
        lock_name = ".njs-publish-" + hashlib.sha256(
            f"{target.id}\0{asset.version}".encode("utf-8")
        ).hexdigest()[:16] + ".lock"
        lock_path = final_release.parent / lock_name
        descriptor: int | None = None
        try:
            try:
                descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                winner = self._validate_completed_release(final_release, asset, target)
                if winner is not None:
                    return winner
                raise DownloadError(f"Release publication is already in progress for target '{target.id}'") from None
            if final_release.exists() or final_release.is_symlink():
                winner = self._validate_completed_release(final_release, asset, target)
                if winner is not None:
                    return winner
                raise DownloadError(f"Release publication conflict for target '{target.id}'")
            try:
                # Staging is adjacent, so this is one same-filesystem directory rename.
                os.rename(release_root, final_release)
            except OSError:
                winner = self._validate_completed_release(final_release, asset, target)
                if winner is not None:
                    return winner
                raise DownloadError(f"Could not publish release for target '{target.id}'") from None
            return None
        finally:
            if descriptor is not None:
                os.close(descriptor)
                try:
                    lock_path.unlink()
                except OSError:
                    pass

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
            merge_root = scratch
            if companion.strip_single_root:
                top_level = list(scratch.iterdir())
                if len(top_level) != 1 or not top_level[0].is_dir():
                    raise DownloadError(f"Archive must contain one wrapper directory for target '{target.id}'")
                merge_root = top_level[0]
                if not any(merge_root.iterdir()):
                    raise DownloadError(f"Archive wrapper directory is empty for target '{target.id}'")
            destination = release_root if companion.extract_to == Path(".") else release_root / companion.extract_to
            try:
                destination.mkdir(parents=True, exist_ok=True)
            except OSError:
                raise DownloadError(f"Extraction collision for target '{target.id}'") from None
            existing = {
                path.relative_to(release_root).as_posix().casefold()
                for path in release_root.rglob("*")
                if path != destination
            }
            planned: set[str] = set()
            sources = sorted(merge_root.rglob("*"), key=lambda path: (len(path.parts), path.as_posix()))
            for source in sources:
                relative = source.relative_to(merge_root)
                target_path = destination / relative
                collision_key = target_path.relative_to(release_root).as_posix().casefold()
                if collision_key in existing or collision_key in planned:
                    raise DownloadError(f"Extraction collision for target '{target.id}'")
                planned.add(collision_key)
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
        if _unsafe_node(release) or _unsafe_node(metadata_path):
            raise DownloadError(f"Unsafe completed release for target '{target.id}'")
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            _validate_metadata_schema(metadata, target.id)
            algorithm, expected = asset.canonical_checksum
            if (
                metadata.get("target_id") != target.id
                or metadata.get("primary_version") != asset.version
                or metadata.get("primary_filename") != asset.filename
            ):
                return None
            if not primary_path.is_file() or _unsafe_node(primary_path):
                raise DownloadError(f"Release file inventory mismatch for target '{target.id}'")
            if (
                metadata.get("primary_checksum_algorithm") != algorithm
                or metadata.get("primary_checksum") != expected
            ):
                raise DownloadError(f"Artifact checksum conflict for target '{target.id}'")
            _validate_artifact_records(metadata, target, asset)
            actual = _hash_file(primary_path, algorithm)
            if actual != expected:
                raise DownloadError(f"Artifact checksum conflict for target '{target.id}'")
            recorded: dict[str, dict[str, Any]] = {}
            for item in metadata["files"]:
                relative = item["path"]
                if relative in recorded:
                    raise DownloadError(f"Release metadata contains duplicate paths for target '{target.id}'")
                recorded[relative] = item
            actual_files = _release_files(release, target.id)
            if set(actual_files) != set(recorded):
                raise DownloadError(f"Release file inventory mismatch for target '{target.id}'")
            for relative, path in actual_files.items():
                item = recorded[relative]
                if path.stat().st_size != item["size"] or _hash_file(path, "sha256") != item["sha256"]:
                    raise DownloadError(f"Release file integrity check failed for target '{target.id}'")
            for artifact_record in metadata.get("artifacts", []):
                artifact_path = actual_files.get(artifact_record["path"])
                if (
                    artifact_path is None
                    or artifact_path.stat().st_size != artifact_record["size"]
                    or _hash_file(artifact_path, artifact_record["checksum_algorithm"])
                    != artifact_record["checksum"]
                ):
                    raise DownloadError(f"Release artifact integrity check failed for target '{target.id}'")
            return DownloadResult(primary_path, asset.filename, primary_path.stat().st_size, algorithm, actual, DownloadDisposition.REUSED)
        except DownloadError:
            raise
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError, AttributeError):
            raise DownloadError(f"Release metadata is invalid for target '{target.id}'") from None

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


def _validate_final_path_compatibility(path: Path, target_id: str) -> None:
    if sys.platform == "win32" and len(str(path)) >= 240:
        raise DownloadError(f"Final artifact path is too long for target '{target_id}'")


def _release_files(root: Path, target_id: str) -> dict[str, Path]:
    result: dict[str, Path] = {}
    casefolded: set[str] = set()
    root_resolved = root.resolve(strict=True)
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.name == METADATA_NAME and path.parent == root:
            continue
        if _unsafe_node(path):
            raise DownloadError(f"Unsafe release filesystem entry for target '{target_id}'")
        relative = path.relative_to(root).as_posix()
        _validate_archive_path(relative, target_id)
        if path.resolve(strict=True).is_relative_to(root_resolved) is False:
            raise DownloadError(f"Release path escapes its root for target '{target_id}'")
        if path.is_dir():
            continue
        if not path.is_file():
            raise DownloadError(f"Unsafe release filesystem entry for target '{target_id}'")
        if path.stat().st_nlink > 1:
            raise DownloadError(f"Unsafe release hard link for target '{target_id}'")
        if relative in result:
            raise DownloadError(f"Duplicate release path for target '{target_id}'")
        if relative.casefold() in casefolded:
            raise DownloadError(f"Case-insensitive release path collision for target '{target_id}'")
        result[relative] = path
        casefolded.add(relative.casefold())
    return result


def _inventory_release(root: Path, roles: dict[str, str], target_id: str) -> list[dict[str, Any]]:
    files = _release_files(root, target_id)
    return [
        {
            "path": relative,
            "sha256": _hash_file(path, "sha256"),
            "size": path.stat().st_size,
            "role": roles.get(relative, "extracted"),
        }
        for relative, path in sorted(files.items())
    ]


def _validate_metadata_schema(value: Any, target_id: str) -> None:
    import re

    legacy_fields = {
        "schema_version", "target_id", "primary_version", "primary_filename",
        "primary_checksum_algorithm", "primary_checksum", "companions", "completed_at", "files",
    }
    if not isinstance(value, dict) or set(value) not in {frozenset(legacy_fields), frozenset(legacy_fields | {"artifacts"})}:
        raise DownloadError(f"Release metadata is invalid for target '{target_id}'")
    if value["schema_version"] != 1:
        raise DownloadError(f"Release metadata is invalid for target '{target_id}'")
    for key in ("target_id", "primary_version", "primary_filename", "primary_checksum_algorithm", "primary_checksum", "completed_at"):
        if not isinstance(value[key], str) or not value[key]:
            raise DownloadError(f"Release metadata is invalid for target '{target_id}'")
    checksum_lengths = {"md5": 32, "sha1": 40, "sha256": 64}
    algorithm = value["primary_checksum_algorithm"]
    if algorithm not in checksum_lengths or re.fullmatch(
        rf"[0-9a-f]{{{checksum_lengths.get(algorithm, 0)}}}", value["primary_checksum"]
    ) is None:
        raise DownloadError(f"Release metadata is invalid for target '{target_id}'")
    try:
        completed = datetime.fromisoformat(value["completed_at"])
    except ValueError:
        raise DownloadError(f"Release metadata is invalid for target '{target_id}'") from None
    if completed.tzinfo is None or completed.utcoffset() is None:
        raise DownloadError(f"Release metadata is invalid for target '{target_id}'")
    if not isinstance(value["companions"], list) or not isinstance(value["files"], list):
        raise DownloadError(f"Release metadata is invalid for target '{target_id}'")
    for companion in value["companions"]:
        legacy_companion_fields = {
            "id", "filename", "action", "sha256", "size", "archive_retained", "extract_to"
        }
        if not isinstance(companion, dict) or set(companion) not in {
            frozenset(legacy_companion_fields),
            frozenset(legacy_companion_fields | {"strip_single_root"}),
        }:
            raise DownloadError(f"Release metadata is invalid for target '{target_id}'")
        if (
            any(not isinstance(companion[key], str) or not companion[key] for key in ("id", "filename", "action", "sha256"))
            or re.fullmatch(r"[0-9a-f]{64}", companion["sha256"]) is None
            or companion["action"] not in {"copy", "extract_7z"}
            or not isinstance(companion["size"], int) or isinstance(companion["size"], bool) or companion["size"] < 0
            or not isinstance(companion["archive_retained"], bool)
            or (companion["extract_to"] is not None and not isinstance(companion["extract_to"], str))
            or ("strip_single_root" in companion and not isinstance(companion["strip_single_root"], bool))
        ):
            raise DownloadError(f"Release metadata is invalid for target '{target_id}'")
    if "artifacts" in value:
        if not isinstance(value["artifacts"], list) or not value["artifacts"]:
            raise DownloadError(f"Release metadata is invalid for target '{target_id}'")
        seen_ids: set[str] = set()
        seen_paths: set[str] = set()
        for artifact in value["artifacts"]:
            if not isinstance(artifact, dict) or set(artifact) != {
                "id", "artifact_id", "role", "filename", "checksum_algorithm",
                "checksum", "size", "path",
            }:
                raise DownloadError(f"Release metadata is invalid for target '{target_id}'")
            if any(not isinstance(artifact[key], str) or not artifact[key] for key in (
                "id", "artifact_id", "role", "filename", "checksum_algorithm", "checksum", "path"
            )):
                raise DownloadError(f"Release metadata is invalid for target '{target_id}'")
            if artifact["role"] not in {"primary", "secondary"}:
                raise DownloadError(f"Release metadata is invalid for target '{target_id}'")
            algorithm = artifact["checksum_algorithm"]
            lengths = {"md5": 32, "sha1": 40, "sha256": 64}
            if algorithm not in lengths or re.fullmatch(rf"[0-9a-f]{{{lengths.get(algorithm, 0)}}}", artifact["checksum"]) is None:
                raise DownloadError(f"Release metadata is invalid for target '{target_id}'")
            if not isinstance(artifact["size"], int) or isinstance(artifact["size"], bool) or artifact["size"] < 0:
                raise DownloadError(f"Release metadata is invalid for target '{target_id}'")
            try:
                _validate_archive_path(artifact["path"], target_id)
            except DownloadError:
                raise DownloadError(f"Release metadata is invalid for target '{target_id}'") from None
            if artifact["id"] in seen_ids or artifact["path"].casefold() in seen_paths:
                raise DownloadError(f"Release metadata contains duplicate artifact entries for target '{target_id}'")
            seen_ids.add(artifact["id"])
            seen_paths.add(artifact["path"].casefold())
    for item in value["files"]:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "size", "role"}:
            raise DownloadError(f"Release metadata is invalid for target '{target_id}'")
        path = item.get("path")
        if not isinstance(path, str):
            raise DownloadError(f"Release metadata is invalid for target '{target_id}'")
        try:
            _validate_archive_path(path, target_id)
        except DownloadError:
            raise DownloadError(f"Release metadata path is invalid for target '{target_id}'") from None
        if "\\" in path or Path(path).as_posix() != path:
            raise DownloadError(f"Release metadata path is invalid for target '{target_id}'")
        if (
            not isinstance(item.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is None
            or not isinstance(item.get("size"), int)
            or isinstance(item["size"], bool)
            or item["size"] < 0
            or item.get("role") not in {"primary", "secondary", "companion", "extracted"}
        ):
            raise DownloadError(f"Release metadata is invalid for target '{target_id}'")


def _validate_artifact_records(metadata: dict[str, Any], target: TargetConfig, asset: NexusAsset) -> None:
    artifacts = metadata.get("artifacts")
    if artifacts is None:
        if target.release_artifacts:
            raise DownloadError(f"Release artifact inventory mismatch for target '{target.id}'")
        return
    expected = [("primary", target.nexus.artifact_id, "primary", asset.filename)] + [
        (
            configured.id,
            configured.artifact_id,
            "secondary",
            f"{configured.artifact_id}-{asset.version}"
            + (f"-{target.artifact.classifier}" if target.artifact.classifier else "")
            + f".{target.artifact.extension}",
        )
        for configured in target.release_artifacts
    ]
    actual = [(item["id"], item["artifact_id"], item["role"], item["filename"]) for item in artifacts]
    if actual != expected or any(item["path"] != item["filename"] for item in artifacts):
        raise DownloadError(f"Release artifact inventory mismatch for target '{target.id}'")
