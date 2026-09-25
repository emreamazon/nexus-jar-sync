"""Load and validate nexus-jar-sync YAML configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from numbers import Real
import os
from pathlib import Path
from typing import Any, Mapping

import yaml


class ConfigError(Exception):
    """Raised when application configuration is missing or invalid."""


@dataclass(frozen=True)
class NetworkConfig:
    timeout_seconds: float = 30
    retries: int = 3
    retry_delay_seconds: float = 5
    verify_tls: bool = True
    ca_bundle: Path | None = None


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    file: Path = Path("logs/nexus-jar-sync.log")
    max_file_size_mb: float = 5
    backup_count: int = 3


@dataclass(frozen=True)
class StateConfig:
    directory: Path = Path("data/state")


@dataclass(frozen=True, repr=False)
class AuthConfig:
    username_env: str | None = None
    password_env: str | None = None
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        return (
            "AuthConfig("
            f"username_env={self.username_env!r}, password_env={self.password_env!r}, "
            "username=<redacted>, password=<redacted>)"
        )


@dataclass(frozen=True)
class ArtifactConfig:
    extension: str = "jar"
    classifier: str | None = None


@dataclass(frozen=True)
class ToolsConfig:
    seven_zip_executable: Path | None = None
    extraction_timeout_seconds: float = 300


@dataclass(frozen=True)
class CompanionConfig:
    id: str
    url: str
    filename: str
    action: str
    keep_archive: bool = True
    extract_to: Path = Path(".")
    auth: AuthConfig | None = None
    strip_single_root: bool = False


@dataclass(frozen=True)
class ReleaseArtifactConfig:
    id: str
    artifact_id: str


@dataclass(frozen=True)
class NexusConfig:
    url: str
    repository: str
    group_id: str
    artifact_id: str


@dataclass(frozen=True)
class DestinationConfig:
    directory: Path


@dataclass(frozen=True)
class TargetConfig:
    id: str
    enabled: bool
    nexus: NexusConfig
    destination: DestinationConfig
    network: NetworkConfig
    auth: AuthConfig
    artifact: ArtifactConfig
    companions: tuple[CompanionConfig, ...] = ()
    release_artifacts: tuple[ReleaseArtifactConfig, ...] = ()


@dataclass(frozen=True)
class AppConfig:
    targets: tuple[TargetConfig, ...]
    logging: LoggingConfig = LoggingConfig()
    state: StateConfig = StateConfig()
    tools: ToolsConfig = ToolsConfig()

    @property
    def enabled_targets(self) -> tuple[TargetConfig, ...]:
        return tuple(target for target in self.targets if target.enabled)


_DEFAULT_NETWORK: dict[str, Any] = {
    "timeout_seconds": 30,
    "retries": 3,
    "retry_delay_seconds": 5,
    "verify_tls": True,
    "ca_bundle": None,
}
_DEFAULT_AUTH: dict[str, Any] = {"username_env": None, "password_env": None}
_AUTH_KEYS = frozenset(_DEFAULT_AUTH)
_DEFAULT_ARTIFACT: dict[str, Any] = {"extension": "jar", "classifier": None}
_DEFAULT_LOGGING: dict[str, Any] = {
    "level": "INFO",
    "file": "logs/nexus-jar-sync.log",
    "max_file_size_mb": 5,
    "backup_count": 3,
}
_DEFAULT_STATE: dict[str, Any] = {"directory": "data/state"}
_DEFAULT_TOOLS: dict[str, Any] = {
    "seven_zip_executable": None,
    "extraction_timeout_seconds": 300,
}


def load_config(path: str | Path) -> AppConfig:
    """Load a YAML configuration file into validated typed objects."""
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(f"Configuration file not found: {config_path}")

    try:
        with config_path.open("r", encoding="utf-8") as config_file:
            raw = yaml.safe_load(config_file)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in configuration file: {exc}") from None
    except OSError as exc:
        raise ConfigError(f"Could not read configuration file: {exc}") from None

    root = _mapping(raw, "Configuration root")
    if "targets" not in root:
        raise ConfigError("Missing required 'targets' list")
    raw_targets = root["targets"]
    if not isinstance(raw_targets, list):
        raise ConfigError("'targets' must be a list")
    if not raw_targets:
        raise ConfigError("'targets' must contain at least one target")

    defaults = _mapping(root.get("defaults", {}), "'defaults'")
    default_network = _merged_section(_DEFAULT_NETWORK, defaults, "network", "defaults")
    default_auth = _merged_section(_DEFAULT_AUTH, defaults, "auth", "defaults")
    default_artifact = _merged_section(_DEFAULT_ARTIFACT, defaults, "artifact", "defaults")
    if "target" in defaults:
        raise ConfigError("'defaults.target' is obsolete; deployed artifacts are append-only")
    logging_config = _parse_logging(_merged_section(_DEFAULT_LOGGING, root, "logging", "root"))
    state_config = _parse_state(_merged_section(_DEFAULT_STATE, root, "state", "root"))
    tools_config = _parse_tools(_merged_section(_DEFAULT_TOOLS, root, "tools", "root"))

    targets: list[TargetConfig] = []
    seen_ids: set[str] = set()
    for index, raw_target in enumerate(raw_targets):
        target = _parse_target(
            _mapping(raw_target, f"Target at index {index}"),
            index,
            default_network,
            default_auth,
            default_artifact,
        )
        if target.id in seen_ids:
            raise ConfigError(f"Duplicate target id: '{target.id}'")
        seen_ids.add(target.id)
        targets.append(target)
    if any(
        companion.action == "extract_7z"
        for target in targets
        for companion in target.companions
    ) and tools_config.seven_zip_executable is None:
        raise ConfigError("'tools.seven_zip_executable' is required for extract_7z companions")
    return AppConfig(
        targets=tuple(targets),
        logging=logging_config,
        state=state_config,
        tools=tools_config,
    )


def _parse_logging(values: Mapping[str, Any]) -> LoggingConfig:
    level = values.get("level")
    if not isinstance(level, str) or level.upper() not in {
        "DEBUG",
        "INFO",
        "WARNING",
        "ERROR",
        "CRITICAL",
    }:
        raise ConfigError(
            "'logging.level' must be DEBUG, INFO, WARNING, ERROR, or CRITICAL"
        )
    file_value = values.get("file")
    if not isinstance(file_value, str) or not file_value.strip():
        raise ConfigError("'logging.file' must be a non-empty path")
    max_size = values.get("max_file_size_mb")
    if not _is_finite_real(max_size) or max_size <= 0:
        raise ConfigError("'logging.max_file_size_mb' must be a finite number greater than 0")
    backup_count = values.get("backup_count")
    if not _is_int(backup_count) or backup_count < 0:
        raise ConfigError("'logging.backup_count' must be an integer of at least 0")
    return LoggingConfig(
        level=level.upper(),
        file=Path(file_value.strip()),
        max_file_size_mb=max_size,
        backup_count=backup_count,
    )


def _parse_state(values: Mapping[str, Any]) -> StateConfig:
    directory = values.get("directory")
    if not isinstance(directory, str) or not directory.strip():
        raise ConfigError("'state.directory' must be a non-empty path")
    return StateConfig(directory=Path(directory.strip()))


def _parse_target(
    raw: Mapping[str, Any],
    index: int,
    default_network: Mapping[str, Any],
    default_auth: Mapping[str, Any],
    default_artifact: Mapping[str, Any],
) -> TargetConfig:
    target_id = _required_text(raw, "id", f"Target at index {index}")
    if "target" in raw:
        raise ConfigError(
            f"'target.keep_previous_versions' is obsolete for target '{target_id}'; "
            "deployed artifacts are append-only"
        )
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError(f"'enabled' must be a boolean for target '{target_id}'")

    nexus_raw = _mapping(raw.get("nexus"), f"'nexus' for target '{target_id}'")
    url = _required_text(nexus_raw, "url", f"Nexus config for target '{target_id}'").rstrip("/")
    if not url:
        raise ConfigError(f"Missing or empty 'url' in Nexus config for target '{target_id}'")
    nexus = NexusConfig(
        url=url,
        repository=_required_text(nexus_raw, "repository", f"Nexus config for target '{target_id}'"),
        group_id=_required_text(nexus_raw, "group_id", f"Nexus config for target '{target_id}'"),
        artifact_id=_required_text(nexus_raw, "artifact_id", f"Nexus config for target '{target_id}'"),
    )

    destination_raw = _mapping(
        raw.get("destination"), f"'destination' for target '{target_id}'"
    )
    destination = DestinationConfig(
        directory=Path(
            _required_text(
                destination_raw, "directory", f"Destination config for target '{target_id}'"
            )
        )
    )

    network_values = _merged_section(default_network, raw, "network", f"target '{target_id}'")
    auth_values = _merged_section(default_auth, raw, "auth", f"target '{target_id}'")
    artifact_values = _merged_section(default_artifact, raw, "artifact", f"target '{target_id}'")
    companions = _parse_companions(raw.get("companions", []), target_id, auth_values)
    release_artifacts = _parse_release_artifacts(
        raw.get("release_artifacts", []), target_id, nexus.artifact_id, artifact_values
    )
    companion_ids = {item.id for item in companions}
    release_ids = {item.id for item in release_artifacts}
    if companion_ids & release_ids:
        raise ConfigError(f"Duplicate release member id for target '{target_id}'")

    return TargetConfig(
        id=target_id,
        enabled=enabled,
        nexus=nexus,
        destination=destination,
        network=_parse_network(network_values, target_id),
        auth=_parse_auth(auth_values, target_id),
        artifact=_parse_artifact(artifact_values, target_id),
        companions=companions,
        release_artifacts=release_artifacts,
    )


def _parse_tools(values: Mapping[str, Any]) -> ToolsConfig:
    _validate_allowed_keys(
        values,
        frozenset({"seven_zip_executable", "extraction_timeout_seconds"}),
        "'tools'",
    )
    executable = values.get("seven_zip_executable")
    if executable is not None and (not isinstance(executable, str) or not executable.strip()):
        raise ConfigError("'tools.seven_zip_executable' must be a non-empty path or null")
    timeout = values.get("extraction_timeout_seconds")
    if not _is_finite_real(timeout) or timeout <= 0:
        raise ConfigError("'tools.extraction_timeout_seconds' must be a finite number greater than 0")
    return ToolsConfig(Path(executable.strip()) if executable is not None else None, timeout)


def _parse_companions(
    value: Any, target_id: str, default_auth: Mapping[str, Any]
) -> tuple[CompanionConfig, ...]:
    from urllib.parse import urlsplit

    if not isinstance(value, list):
        raise ConfigError(f"'companions' must be a list for target '{target_id}'")
    result: list[CompanionConfig] = []
    seen: set[str] = set()
    allowed = frozenset({"id", "url", "filename", "action", "keep_archive", "extract_to", "auth", "strip_single_root"})
    for index, item in enumerate(value):
        raw = _mapping(item, f"Companion at index {index} for target '{target_id}'")
        _validate_allowed_keys(raw, allowed, f"companion in target '{target_id}'")
        companion_id = _required_text(raw, "id", f"companion in target '{target_id}'")
        if companion_id in seen:
            raise ConfigError(f"Duplicate companion id '{companion_id}' for target '{target_id}'")
        seen.add(companion_id)
        url = _required_text(raw, "url", f"companion '{companion_id}'")
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ConfigError(f"Companion URL is invalid for target '{target_id}'")
        filename = _required_text(raw, "filename", f"companion '{companion_id}'")
        if not _safe_relative_path(filename, allow_subdirectories=False):
            raise ConfigError(f"Companion filename is unsafe for target '{target_id}'")
        if filename == ".nexus-jar-sync-release.json":
            raise ConfigError(f"Companion filename is reserved for target '{target_id}'")
        action = _required_text(raw, "action", f"companion '{companion_id}'")
        if action not in {"copy", "extract_7z"}:
            raise ConfigError(f"Companion action is invalid for target '{target_id}'")
        keep = raw.get("keep_archive", True)
        if not isinstance(keep, bool):
            raise ConfigError(f"'keep_archive' must be a boolean for target '{target_id}'")
        strip_single_root = raw.get("strip_single_root", False)
        if not isinstance(strip_single_root, bool):
            raise ConfigError(f"'strip_single_root' must be a boolean for target '{target_id}'")
        if strip_single_root and action != "extract_7z":
            raise ConfigError(f"'strip_single_root' requires extract_7z for target '{target_id}'")
        extract_text = raw.get("extract_to", ".")
        if not isinstance(extract_text, str) or not _safe_relative_path(extract_text, allow_subdirectories=True):
            raise ConfigError(f"'extract_to' is unsafe for target '{target_id}'")
        auth_values = _merged_section(default_auth, raw, "auth", f"companion '{companion_id}'")
        auth = _parse_auth(auth_values, target_id) if "auth" in raw else None
        result.append(CompanionConfig(companion_id, url, filename, action, keep, Path(extract_text), auth, strip_single_root))
    return tuple(result)


def _parse_release_artifacts(
    value: Any,
    target_id: str,
    primary_artifact_id: str,
    artifact_values: Mapping[str, Any],
) -> tuple[ReleaseArtifactConfig, ...]:
    if not isinstance(value, list):
        raise ConfigError(f"'release_artifacts' must be a list for target '{target_id}'")
    result: list[ReleaseArtifactConfig] = []
    seen_ids: set[str] = {"primary"}
    seen_coordinates = {primary_artifact_id}
    _parse_artifact(artifact_values, target_id)
    for index, item in enumerate(value):
        raw = _mapping(item, f"Release artifact at index {index} for target '{target_id}'")
        _validate_allowed_keys(raw, frozenset({"id", "artifact_id"}), f"release artifact in target '{target_id}'")
        artifact_id = _required_text(raw, "artifact_id", f"release artifact in target '{target_id}'")
        release_id = _required_text(raw, "id", f"release artifact in target '{target_id}'")
        if release_id in seen_ids:
            raise ConfigError(f"Duplicate release artifact id '{release_id}' for target '{target_id}'")
        if artifact_id in seen_coordinates:
            raise ConfigError(f"Duplicate release artifact coordinates for target '{target_id}'")
        # Inherited extension/classifier make distinct artifact IDs produce distinct filenames.
        if not _safe_relative_path(artifact_id, allow_subdirectories=False):
            raise ConfigError(f"Release artifact id is unsafe for target '{target_id}'")
        seen_ids.add(release_id)
        seen_coordinates.add(artifact_id)
        result.append(ReleaseArtifactConfig(release_id, artifact_id))
    return tuple(result)


def _parse_network(values: Mapping[str, Any], target_id: str) -> NetworkConfig:
    timeout = values.get("timeout_seconds")
    retries = values.get("retries")
    retry_delay = values.get("retry_delay_seconds")
    verify_tls = values.get("verify_tls")
    ca_bundle = values.get("ca_bundle")
    if not _is_finite_real(timeout) or timeout <= 0:
        raise ConfigError(
            f"'timeout_seconds' must be a finite number greater than 0 for target '{target_id}'"
        )
    if not _is_int(retries) or retries < 0:
        raise ConfigError(f"'retries' must be an integer of at least 0 for target '{target_id}'")
    if not _is_finite_real(retry_delay) or retry_delay < 0:
        raise ConfigError(
            f"'retry_delay_seconds' must be a finite number greater than or equal to 0 "
            f"for target '{target_id}'"
        )
    if not isinstance(verify_tls, bool):
        raise ConfigError(f"'verify_tls' must be a boolean for target '{target_id}'")
    if ca_bundle is not None and (not isinstance(ca_bundle, str) or not ca_bundle.strip()):
        raise ConfigError(f"'ca_bundle' must be a path or null for target '{target_id}'")
    return NetworkConfig(
        timeout_seconds=timeout,
        retries=retries,
        retry_delay_seconds=retry_delay,
        verify_tls=verify_tls,
        ca_bundle=Path(ca_bundle) if ca_bundle is not None else None,
    )


def _parse_auth(values: Mapping[str, Any], target_id: str) -> AuthConfig:
    username_env = _optional_env_name(values.get("username_env"), "username_env", target_id)
    password_env = _optional_env_name(values.get("password_env"), "password_env", target_id)
    username = _resolve_env(username_env, target_id)
    password = _resolve_env(password_env, target_id)
    return AuthConfig(username_env, password_env, username, password)


def _parse_artifact(values: Mapping[str, Any], target_id: str) -> ArtifactConfig:
    extension = values.get("extension")
    classifier = values.get("classifier")
    if not isinstance(extension, str) or not extension.strip():
        raise ConfigError(f"'extension' must be a non-empty string for target '{target_id}'")
    if classifier is not None and (not isinstance(classifier, str) or not classifier.strip()):
        raise ConfigError(f"'classifier' must be a non-empty string or null for target '{target_id}'")
    return ArtifactConfig(extension=extension.strip(), classifier=classifier.strip() if classifier else None)


def _merged_section(
    base: Mapping[str, Any], container: Mapping[str, Any], key: str, context: str
) -> dict[str, Any]:
    override = _mapping(container.get(key, {}), f"'{key}' in {context}")
    if key == "auth":
        _validate_allowed_keys(override, _AUTH_KEYS, f"'auth' in {context}")
    return {**base, **override}


def _validate_allowed_keys(
    values: Mapping[str, Any], allowed: frozenset[str], context: str
) -> None:
    for field_name in values:
        if field_name not in allowed:
            raise ConfigError(f"Unknown field '{field_name}' in {context}")


def _mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{context} must be a mapping")
    return value


def _required_text(values: Mapping[str, Any], key: str, context: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Missing or empty '{key}' in {context}")
    return value.strip()


def _optional_env_name(value: Any, key: str, target_id: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"'{key}' must be a non-empty string or null for target '{target_id}'")
    return value.strip()


def _resolve_env(name: str | None, target_id: str) -> str | None:
    if name is None:
        return None
    value = os.environ.get(name)
    if value is None:
        raise ConfigError(f"Environment variable {name} is required for target '{target_id}'")
    return value


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_finite_real(value: Any) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value)


def _safe_relative_path(value: str, *, allow_subdirectories: bool) -> bool:
    import re
    from pathlib import PurePosixPath, PureWindowsPath

    if not value or "\x00" in value or any(ord(character) < 32 for character in value):
        return False
    if PureWindowsPath(value).is_absolute() or PureWindowsPath(value).drive or PurePosixPath(value).is_absolute():
        return False
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    if not allow_subdirectories and len(parts) != 1:
        return False
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
    for part in parts:
        if not part or part == ".." or (part == "." and normalized != "."):
            return False
        if part != "." and (
            part[-1] in {" ", "."}
            or any(character in '<>:"|?*' for character in part)
            or part.split(".", 1)[0].upper() in reserved
            or re.match(r"^[A-Za-z]:", part)
        ):
            return False
    return True
