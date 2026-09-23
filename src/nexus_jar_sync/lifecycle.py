"""Conservative target-specific local artifact retention."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import re

from packaging.version import InvalidVersion, Version

from nexus_jar_sync.config import TargetConfig


class LifecycleError(Exception):
    """Raised when local artifact retention cannot be applied safely."""


class ArtifactLifecycleManager:
    """Apply fail-fast retention to direct files belonging to one target."""

    def apply_retention(self, target: TargetConfig, current_path: str | Path) -> tuple[Path, ...]:
        destination = target.destination.directory.resolve(strict=False)
        current_input = Path(current_path)
        current = current_input.resolve(strict=False)
        if (
            current.parent != destination
            or not current_input.exists()
            or not current_input.is_file()
            or current_input.is_symlink()
        ):
            raise LifecycleError(f"Current artifact path is invalid for target '{target.id}'")

        pattern = self._managed_pattern(target)
        current_match = pattern.fullmatch(current.name)
        if current_match is None:
            raise LifecycleError(
                f"Current artifact filename does not match target '{target.id}' coordinates"
            )

        try:
            entries = sorted(destination.iterdir(), key=lambda path: path.name)
        except OSError:
            raise LifecycleError(
                f"Could not inspect destination for target '{target.id}'"
            ) from None

        candidates: list[tuple[Path, str, Version]] = []
        versions: dict[Version, set[str]] = defaultdict(set)
        for entry in entries:
            if entry.is_symlink() or not entry.is_file():
                continue
            match = pattern.fullmatch(entry.name)
            if match is None:
                continue
            raw_version = match.group("version")
            try:
                parsed_version = Version(raw_version)
            except InvalidVersion:
                continue
            candidates.append((entry, raw_version, parsed_version))
            versions[parsed_version].add(raw_version)

        keep_count = 1 + target.retention.keep_previous_versions
        ordered_versions = sorted(versions, reverse=True)
        retained_versions = set(ordered_versions[:keep_count])
        try:
            current_version = Version(current_match.group("version"))
        except InvalidVersion:
            current_version = None
        if current_version is not None:
            retained_versions.add(current_version)

        removed: list[Path] = []
        for path, _raw_version, parsed_version in candidates:
            if path.resolve(strict=False) == current:
                continue
            if parsed_version in retained_versions:
                continue
            if len(versions[parsed_version]) > 1:
                continue
            try:
                path.unlink()
            except OSError:
                raise LifecycleError(
                    f"Could not remove '{path.name}' for target '{target.id}'"
                ) from None
            removed.append(path)
        return tuple(removed)

    @staticmethod
    def _managed_pattern(target: TargetConfig) -> re.Pattern[str]:
        artifact = re.escape(target.nexus.artifact_id)
        extension = re.escape(target.artifact.extension)
        if target.artifact.classifier is None:
            suffix = ""
        else:
            suffix = f"-{re.escape(target.artifact.classifier)}"
        return re.compile(rf"^{artifact}-(?P<version>.+){suffix}\.{extension}$")
