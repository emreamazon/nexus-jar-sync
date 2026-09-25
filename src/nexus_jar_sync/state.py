"""Per-target persisted state and pure artifact change detection."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping

from nexus_jar_sync.nexus_client import NexusAsset


class StateError(Exception):
    """Raised when per-target state cannot be read, validated, or written."""


@dataclass(frozen=True)
class TargetState:
    version: str
    path: str
    checksum_algorithm: str
    checksum: str
    downloaded_at: str
    release_directory: str | None = None
    metadata_path: str | None = None


class ChangeDecision(Enum):
    FIRST_RUN = "first_run"
    VERSION_CHANGED = "version_changed"
    CHECKSUM_CHANGED = "checksum_changed"
    PATH_CHANGED = "path_changed"
    CURRENT = "current"

    @property
    def update_required(self) -> bool:
        return self is not ChangeDecision.CURRENT


class StateStore:
    """Load and atomically save one JSON state record per target."""

    def __init__(self, state_directory: str | Path) -> None:
        self._state_directory = Path(state_directory)

    def path_for(self, target_id: str) -> Path:
        digest = hashlib.sha256(target_id.encode("utf-8")).hexdigest()
        return self._state_directory / f"{digest}.json"

    def _legacy_path_for(self, target_id: str) -> Path:
        readable = re.sub(r"[^A-Za-z0-9._-]+", "_", target_id).strip("._-") or "target"
        readable = readable[:60]
        digest = hashlib.sha256(target_id.encode("utf-8")).hexdigest()
        return self._state_directory / f"{readable}-{digest}.json"

    def load(self, target_id: str) -> TargetState | None:
        path = self.path_for(target_id)
        if not path.exists():
            path = self._legacy_path_for(target_id)
            if not path.exists():
                return None
        try:
            with path.open("r", encoding="utf-8") as state_file:
                value = json.load(state_file)
        except json.JSONDecodeError:
            raise StateError(f"State file is invalid JSON for target '{target_id}'") from None
        except OSError as exc:
            raise StateError(f"Could not read state for target '{target_id}': {exc}") from None
        return _state_from_mapping(value, target_id)

    def save(self, target_id: str, state: TargetState) -> None:
        validated = _state_from_mapping(asdict(state), target_id)
        path = self.path_for(target_id)
        temporary_path: Path | None = None
        try:
            self._state_directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._state_directory,
                prefix=".njs-state-",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                json.dump(asdict(validated), temporary_file, indent=2, sort_keys=True)
                temporary_file.write("\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, path)
        except (OSError, TypeError, ValueError) as exc:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise StateError(f"Could not save state for target '{target_id}': {exc}") from None


def determine_change(
    state: TargetState | None, asset: NexusAsset, expected_path: str | Path | None = None
) -> ChangeDecision:
    """Compare stored state with a discovery result without filesystem effects."""
    if state is None:
        return ChangeDecision.FIRST_RUN
    if state.version != asset.version:
        return ChangeDecision.VERSION_CHANGED
    compared_path = asset.path if expected_path is None else expected_path
    if Path(state.path).resolve(strict=False) != Path(compared_path).resolve(strict=False):
        return ChangeDecision.PATH_CHANGED
    algorithm = state.checksum_algorithm.lower()
    current_checksum = asset.checksums.get(algorithm)
    if current_checksum is None or current_checksum.lower() != state.checksum.lower():
        return ChangeDecision.CHECKSUM_CHANGED
    return ChangeDecision.CURRENT


def _state_from_mapping(value: Any, target_id: str) -> TargetState:
    if not isinstance(value, dict):
        raise StateError(f"State root must be an object for target '{target_id}'")
    fields = ("version", "path", "checksum_algorithm", "checksum", "downloaded_at")
    for field_name in fields:
        field_value = value.get(field_name)
        if not isinstance(field_value, str) or not field_value.strip():
            raise StateError(f"State field '{field_name}' is invalid for target '{target_id}'")
    for field_name in ("release_directory", "metadata_path"):
        field_value = value.get(field_name)
        if field_value is not None and (not isinstance(field_value, str) or not field_value.strip()):
            raise StateError(f"State field '{field_name}' is invalid for target '{target_id}'")
    algorithm = value["checksum_algorithm"].strip().lower()
    if algorithm not in {"sha256", "sha1", "md5"}:
        raise StateError(
            f"State field 'checksum_algorithm' is invalid for target '{target_id}'"
        )
    try:
        timestamp = datetime.fromisoformat(value["downloaded_at"])
    except ValueError:
        raise StateError(f"State field 'downloaded_at' is invalid for target '{target_id}'") from None
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise StateError(
            f"State field 'downloaded_at' must include a timezone for target '{target_id}'"
        )
    return TargetState(
        version=value["version"].strip(),
        path=value["path"].strip(),
        checksum_algorithm=algorithm,
        checksum=value["checksum"].strip().lower(),
        downloaded_at=value["downloaded_at"],
        release_directory=value.get("release_directory"),
        metadata_path=value.get("metadata_path"),
    )
