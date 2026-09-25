from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

from nexus_jar_sync.config import load_config


SCRIPT = Path(__file__).parents[1] / "deployment" / "offline" / "generate_windows_config.py"
SPEC = importlib.util.spec_from_file_location("windows_config_generator", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def arguments(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--output", str((tmp_path / "config with spaces" / "config.yaml").resolve()),
        "--nexus-url", "http://nexus.example.invalid", "--repository", "maven releases",
        "--group-id", "tr.com.example.project", "--primary-artifact-id", "windows-versions",
        "--windows-obs", "windows-obs", "--linux-versions", "linux-versions",
        "--linux-obs", "linux-obs", "--destination", str((tmp_path / "Yayınlar & (prod)").resolve()),
        "--dependencies-url", "http://nexus.example.invalid/repository/static/dependencies.7z",
        "--license-url", "http://nexus.example.invalid/repository/static/license.txt",
        "--seven-zip", str((tmp_path / "Approved Tools (x64)" / "7z.exe").resolve()),
        "--log-file", str((tmp_path / "logs" / "nexus sync.log").resolve()),
        "--state-directory", str((tmp_path / "data" / "state").resolve()),
        "--allow-http", *extra,
    ]


def test_anonymous_four_artifact_http_config_is_safely_generated(tmp_path: Path) -> None:
    assert MODULE.main(arguments(tmp_path)) == 0
    output = tmp_path / "config with spaces" / "config.yaml"
    value = yaml.safe_load(output.read_text(encoding="utf-8"))
    target = value["targets"][0]
    assert "auth" not in target
    assert target["nexus"]["artifact_id"] == "windows-versions"
    assert [item["artifact_id"] for item in target["release_artifacts"]] == [
        "windows-obs", "linux-versions", "linux-obs"
    ]
    dependencies = target["companions"][0]
    assert dependencies["strip_single_root"] is True
    assert dependencies["keep_archive"] is False
    assert target["destination"]["directory"].endswith("Yayınlar & (prod)")
    loaded = load_config(output)
    assert len(loaded.targets[0].release_artifacts) == 3
    assert loaded.targets[0].auth.username is None


def test_optional_auth_writes_names_only_and_http_requires_acknowledgement(tmp_path: Path) -> None:
    no_ack = [item for item in arguments(tmp_path) if item != "--allow-http"]
    assert MODULE.main(no_ack) == 2
    assert MODULE.main(arguments(tmp_path, "--username-env", "NEXUS_USER", "--password-env", "NEXUS_PASSWORD")) == 0
    text = (tmp_path / "config with spaces" / "config.yaml").read_text(encoding="utf-8")
    assert "NEXUS_USER" in text and "NEXUS_PASSWORD" in text
    assert "secret" not in text.lower()


@pytest.mark.parametrize("bad", ["bad\nvalue", "bad\x00value"])
def test_control_character_injection_is_rejected(tmp_path: Path, bad: str) -> None:
    values = arguments(tmp_path)
    values[values.index("--repository") + 1] = bad
    assert MODULE.main(values) == 2


def test_plan_only_validates_without_creating_output(tmp_path: Path) -> None:
    assert MODULE.main(arguments(tmp_path, "--plan")) == 0
    assert not (tmp_path / "config with spaces").exists()
