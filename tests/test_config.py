from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from nexus_jar_sync.config import ConfigError, load_config


def target(target_id: str = "one", **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": target_id,
        "nexus": {
            "url": "https://nexus.example.com/",
            "repository": "releases",
            "group_id": "com.example",
            "artifact_id": "application",
        },
        "destination": {"directory": "output"},
    }
    value.update(overrides)
    return value


def write_config(tmp_path: Path, content: dict[str, object]) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(content), encoding="utf-8")
    return path


def test_valid_single_target_uses_built_in_defaults(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, {"targets": [target()]}))
    loaded = config.targets[0]
    assert loaded.id == "one"
    assert loaded.enabled is True
    assert loaded.artifact.extension == "jar"
    assert loaded.artifact.classifier is None
    assert config.logging.level == "INFO"
    assert config.logging.file == Path("logs/nexus-jar-sync.log")
    assert config.logging.max_file_size_mb == 5
    assert config.logging.backup_count == 3
    assert config.state.directory == Path("data/state")


def test_explicit_logging_and_state_configuration(tmp_path: Path) -> None:
    log_file = tmp_path / "not-created" / "sync.log"
    state_directory = tmp_path / "state"
    raw = {
        "logging": {
            "level": "debug",
            "file": str(log_file),
            "max_file_size_mb": 1.5,
            "backup_count": 2,
        },
        "state": {"directory": str(state_directory)},
        "targets": [target()],
    }
    config = load_config(write_config(tmp_path, raw))
    assert config.logging.level == "DEBUG"
    assert config.logging.file == log_file
    assert config.logging.max_file_size_mb == 1.5
    assert config.logging.backup_count == 2
    assert config.state.directory == state_directory
    assert not log_file.parent.exists()
    assert not state_directory.exists()


@pytest.mark.parametrize("level", ["TRACE", "", 1])
def test_invalid_logging_level_fails(tmp_path: Path, level: object) -> None:
    with pytest.raises(ConfigError, match="logging.level"):
        load_config(write_config(tmp_path, {"logging": {"level": level}, "targets": [target()]}))


def test_empty_logging_path_fails(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="logging.file"):
        load_config(write_config(tmp_path, {"logging": {"file": "  "}, "targets": [target()]}))


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True])
def test_invalid_logging_max_size_fails(tmp_path: Path, value: object) -> None:
    with pytest.raises(ConfigError, match="logging.max_file_size_mb"):
        load_config(
            write_config(
                tmp_path,
                {"logging": {"max_file_size_mb": value}, "targets": [target()]},
            )
        )


@pytest.mark.parametrize("value", [-1, 1.5, True])
def test_invalid_logging_backup_count_fails(tmp_path: Path, value: object) -> None:
    with pytest.raises(ConfigError, match="logging.backup_count"):
        load_config(
            write_config(tmp_path, {"logging": {"backup_count": value}, "targets": [target()]})
        )


def test_empty_state_directory_fails(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="state.directory"):
        load_config(write_config(tmp_path, {"state": {"directory": ""}, "targets": [target()]}))


def test_valid_multi_target_configuration(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, {"targets": [target("one"), target("two")]}))
    assert [item.id for item in config.targets] == ["one", "two"]


def test_defaults_are_inherited(tmp_path: Path) -> None:
    raw = {
        "defaults": {"network": {"timeout_seconds": 12, "retries": 7}},
        "targets": [target()],
    }
    loaded = load_config(write_config(tmp_path, raw)).targets[0]
    assert loaded.network.timeout_seconds == 12
    assert loaded.network.retries == 7
    assert loaded.network.retry_delay_seconds == 5


def test_target_overrides_merge_without_erasing_defaults(tmp_path: Path) -> None:
    raw = {
        "defaults": {"network": {"timeout_seconds": 30, "retries": 4}},
        "targets": [target(network={"timeout_seconds": 8})],
    }
    network = load_config(write_config(tmp_path, raw)).targets[0].network
    assert network.timeout_seconds == 8
    assert network.retries == 4


def test_disabled_targets_are_retained_and_filtered(tmp_path: Path) -> None:
    raw = {"targets": [target("enabled"), target("disabled", enabled=False)]}
    config = load_config(write_config(tmp_path, raw))
    assert len(config.targets) == 2
    assert [item.id for item in config.enabled_targets] == ["enabled"]


def test_duplicate_target_ids_fail(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Duplicate target id"):
        load_config(write_config(tmp_path, {"targets": [target(), target()]}))


@pytest.mark.parametrize("field", ["url", "repository", "group_id", "artifact_id"])
def test_missing_nexus_fields_fail(tmp_path: Path, field: str) -> None:
    item = target()
    del item["nexus"][field]  # type: ignore[index]
    with pytest.raises(ConfigError, match=field):
        load_config(write_config(tmp_path, {"targets": [item]}))


def test_missing_destination_fails(tmp_path: Path) -> None:
    item = target()
    del item["destination"]
    with pytest.raises(ConfigError, match="destination"):
        load_config(write_config(tmp_path, {"targets": [item]}))


@pytest.mark.parametrize(
    ("key", "value"),
    [("timeout_seconds", 0), ("retries", -1), ("retry_delay_seconds", -1), ("verify_tls", "yes")],
)
def test_invalid_network_values_fail(tmp_path: Path, key: str, value: object) -> None:
    with pytest.raises(ConfigError, match=key):
        load_config(write_config(tmp_path, {"targets": [target(network={key: value})]}))


@pytest.mark.parametrize("field", ["timeout_seconds", "retry_delay_seconds"])
@pytest.mark.parametrize("yaml_value", [".nan", ".inf", "-.inf"])
def test_non_finite_network_values_fail(
    tmp_path: Path, field: str, yaml_value: str
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"""
targets:
  - id: one
    nexus:
      url: https://nexus.example.com
      repository: releases
      group_id: com.example
      artifact_id: application
    destination:
      directory: output
    network:
      {field}: {yaml_value}
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=field):
        load_config(path)


@pytest.mark.parametrize("value", [-1, 1.5, True])
def test_invalid_retention_fails(tmp_path: Path, value: object) -> None:
    with pytest.raises(ConfigError, match="keep_previous_versions"):
        load_config(
            write_config(tmp_path, {"targets": [target(target={"keep_previous_versions": value})]})
        )


def test_missing_username_environment_variable_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_NEXUS_USER", raising=False)
    path = write_config(tmp_path, {"targets": [target(auth={"username_env": "TEST_NEXUS_USER"})]})
    with pytest.raises(ConfigError, match="TEST_NEXUS_USER"):
        load_config(path)


def test_missing_password_environment_variable_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_NEXUS_PASSWORD", raising=False)
    path = write_config(tmp_path, {"targets": [target(auth={"password_env": "TEST_NEXUS_PASSWORD"})]})
    with pytest.raises(ConfigError, match="TEST_NEXUS_PASSWORD"):
        load_config(path)


def test_credentials_resolve_and_repr_is_redacted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_NEXUS_USER", "alice")
    monkeypatch.setenv("TEST_NEXUS_PASSWORD", "very-secret")
    raw = {
        "targets": [
            target(auth={"username_env": "TEST_NEXUS_USER", "password_env": "TEST_NEXUS_PASSWORD"})
        ]
    }
    auth = load_config(write_config(tmp_path, raw)).targets[0].auth
    assert auth.username == "alice"
    assert auth.password == "very-secret"
    assert "alice" not in repr(auth)
    assert "very-secret" not in repr(auth)


@pytest.mark.parametrize(
    ("context", "field"),
    [
        ("defaults", "username"),
        ("defaults", "password"),
        ("target", "username"),
        ("target", "password"),
        ("target", "user_name_env"),
        ("target", "token"),
    ],
)
def test_unknown_authentication_fields_are_rejected_without_exposing_values(
    tmp_path: Path, context: str, field: str
) -> None:
    secret = "must-never-appear"
    raw: dict[str, object] = {"targets": [target()]}
    if context == "defaults":
        raw["defaults"] = {"auth": {field: secret}}
    else:
        raw["targets"] = [target(auth={field: secret})]
    with pytest.raises(ConfigError) as caught:
        load_config(write_config(tmp_path, raw))
    message = str(caught.value)
    assert field in message
    assert context in message
    assert secret not in message


def test_trailing_slash_is_removed_from_nexus_url(tmp_path: Path) -> None:
    loaded = load_config(write_config(tmp_path, {"targets": [target()]})).targets[0]
    assert loaded.nexus.url == "https://nexus.example.com"


def test_loading_does_not_create_destination_directory(tmp_path: Path) -> None:
    destination = tmp_path / "not-created"
    load_config(write_config(tmp_path, {"targets": [target(destination={"directory": str(destination)})]}))
    assert not destination.exists()


def test_example_configuration_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "NEXUS_USERNAME",
        "NEXUS_PASSWORD",
        "LIBRARY_NEXUS_USERNAME",
        "LIBRARY_NEXUS_PASSWORD",
    ):
        monkeypatch.setenv(name, "fake-test-value")
    path = Path(__file__).parents[1] / "config" / "config.example.yaml"
    config = load_config(path)
    assert len(config.targets) == 3
    assert len(config.enabled_targets) == 2
    assert config.logging.level == "INFO"
    assert config.logging.file == Path("logs/nexus-jar-sync.log")
    assert config.state.directory == Path("data/state")


def test_companion_and_tools_configuration_is_typed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_USER", "user")
    monkeypatch.setenv("NEXUS_PASS", "password")
    value = {
        "tools": {"seven_zip_executable": "C:/Program Files/7-Zip/7z.exe", "extraction_timeout_seconds": 12},
        "defaults": {"auth": {"username_env": "NEXUS_USER", "password_env": "NEXUS_PASS"}},
        "targets": [target(companions=[
            {"id": "deps", "url": "https://nexus.example.invalid/static/deps.7z", "filename": "deps.7z", "action": "extract_7z", "keep_archive": False, "extract_to": "lib"},
            {"id": "license", "url": "http://files.example.invalid/license.txt", "filename": "license.txt", "action": "copy"},
        ])],
    }
    loaded = load_config(write_config(tmp_path, value))
    assert loaded.tools.extraction_timeout_seconds == 12
    assert loaded.targets[0].companions[0].extract_to == Path("lib")
    assert loaded.targets[0].companions[0].keep_archive is False


@pytest.mark.parametrize("url", ["relative/file", "file:///tmp/x", "https:///missing", "https://user:pass@example.invalid/x"])
def test_unsafe_companion_url_is_rejected(tmp_path: Path, url: str) -> None:
    with pytest.raises(ConfigError, match="Companion URL"):
        load_config(write_config(tmp_path, {"targets": [target(companions=[{"id": "one", "url": url, "filename": "one.txt", "action": "copy"}])]}))


@pytest.mark.parametrize("field", ["../x", "C:/x", "CON", "trail.", "a/b"])
def test_unsafe_companion_filename_is_rejected(tmp_path: Path, field: str) -> None:
    with pytest.raises(ConfigError, match="filename is unsafe"):
        load_config(write_config(tmp_path, {"targets": [target(companions=[{"id": "one", "url": "https://example.invalid/x", "filename": field, "action": "copy"}])]}))


def test_unknown_and_duplicate_companions_are_rejected(tmp_path: Path) -> None:
    companion = {"id": "one", "url": "https://example.invalid/x", "filename": "x.txt", "action": "copy"}
    with pytest.raises(ConfigError, match="Unknown field"):
        load_config(write_config(tmp_path, {"targets": [target(companions=[{**companion, "secret": "bad"}])]}))
    with pytest.raises(ConfigError, match="Duplicate companion"):
        load_config(write_config(tmp_path, {"targets": [target(companions=[companion, companion])]}))
