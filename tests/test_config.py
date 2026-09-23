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
    assert loaded.retention.keep_previous_versions == 1


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
    assert len(config.targets) == 2
    assert len(config.enabled_targets) == 1
