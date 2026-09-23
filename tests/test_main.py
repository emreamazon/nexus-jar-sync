from __future__ import annotations

from pathlib import Path

import pytest
import requests
import yaml

from nexus_jar_sync.downloader import ArtifactDownloader
from nexus_jar_sync.lifecycle import ArtifactLifecycleManager
from nexus_jar_sync.main import main
from nexus_jar_sync.sync import SyncService


def test_cli_only_validates_configuration_without_network_or_filesystem_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    destination = tmp_path / "destination"
    state_directory = tmp_path / "state"
    log_directory = tmp_path / "logs"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "logging": {"file": str(log_directory / "sync.log")},
                "state": {"directory": str(state_directory)},
                "targets": [
                    {
                        "id": "example",
                        "nexus": {
                            "url": "https://nexus.example.com",
                            "repository": "releases",
                            "group_id": "com.example",
                            "artifact_id": "application",
                        },
                        "destination": {"directory": str(destination)},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    def reject_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("CLI attempted network access")

    monkeypatch.setattr(requests.sessions.Session, "request", reject_network)
    monkeypatch.setattr(
        ArtifactDownloader,
        "download",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("CLI attempted download")),
    )
    monkeypatch.setattr(
        ArtifactLifecycleManager,
        "apply_retention",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("CLI attempted retention")),
    )
    monkeypatch.setattr(
        SyncService,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("CLI attempted sync")),
    )
    monkeypatch.setattr(
        "time.sleep",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("CLI attempted sleep")),
    )
    unrelated_file = tmp_path / "unrelated.jar"
    unrelated_file.write_bytes(b"keep")
    assert main(["--config", str(config_path)]) == 0
    assert "Enabled targets: 1" in capsys.readouterr().out
    assert not destination.exists()
    assert not state_directory.exists()
    assert not log_directory.exists()
    assert unrelated_file.read_bytes() == b"keep"
