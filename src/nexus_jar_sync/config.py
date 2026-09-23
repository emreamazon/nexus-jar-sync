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
class RetentionConfig:
    keep_previous_versions: int = 1


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
    retention: RetentionConfig


@dataclass(frozen=True)
class AppConfig:
    targets: tuple[TargetConfig, ...]
    logging: LoggingConfig = LoggingConfig()
    state: StateConfig = StateConfig()

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
_DEFAULT_ARTIFACT: dict[str, Any] = {"extension": "jar", "classifier": None}
_DEFAULT_RETENTION: dict[str, Any] = {"keep_previous_versions": 1}
_DEFAULT_LOGGING: dict[str, Any] = {
    "level": "INFO",
    "file": "logs/nexus-jar-sync.log",
    "max_file_size_mb": 5,
    "backup_count": 3,
}
_DEFAULT_STATE: dict[str, Any] = {"directory": "data/state"}


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
    default_retention = _merged_section(_DEFAULT_RETENTION, defaults, "target", "defaults")
    logging_config = _parse_logging(_merged_section(_DEFAULT_LOGGING, root, "logging", "root"))
    state_config = _parse_state(_merged_section(_DEFAULT_STATE, root, "state", "root"))

    targets: list[TargetConfig] = []
    seen_ids: set[str] = set()
    for index, raw_target in enumerate(raw_targets):
        target = _parse_target(
            _mapping(raw_target, f"Target at index {index}"),
            index,
            default_network,
            default_auth,
            default_artifact,
            default_retention,
        )
        if target.id in seen_ids:
            raise ConfigError(f"Duplicate target id: '{target.id}'")
        seen_ids.add(target.id)
        targets.append(target)
    return AppConfig(
        targets=tuple(targets),
        logging=logging_config,
        state=state_config,
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
    default_retention: Mapping[str, Any],
) -> TargetConfig:
    target_id = _required_text(raw, "id", f"Target at index {index}")
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
    retention_values = _merged_section(default_retention, raw, "target", f"target '{target_id}'")

    return TargetConfig(
        id=target_id,
        enabled=enabled,
        nexus=nexus,
        destination=destination,
        network=_parse_network(network_values, target_id),
        auth=_parse_auth(auth_values, target_id),
        artifact=_parse_artifact(artifact_values, target_id),
        retention=_parse_retention(retention_values, target_id),
    )


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


def _parse_retention(values: Mapping[str, Any], target_id: str) -> RetentionConfig:
    keep = values.get("keep_previous_versions")
    if not _is_int(keep) or keep < 0:
        raise ConfigError(
            f"'keep_previous_versions' must be an integer of at least 0 for target '{target_id}'"
        )
    return RetentionConfig(keep_previous_versions=keep)


def _merged_section(
    base: Mapping[str, Any], container: Mapping[str, Any], key: str, context: str
) -> dict[str, Any]:
    override = _mapping(container.get(key, {}), f"'{key}' in {context}")
    return {**base, **override}


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
