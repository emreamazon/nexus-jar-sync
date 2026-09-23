from __future__ import annotations

from pathlib import Path

import yaml

from nexus_jar_sync.main import main
from nexus_jar_sync.sync import SyncSummary, TargetSyncResult, TargetSyncStatus


def test_cli_runs_active_sync_and_creates_only_logging_side_effect(
    tmp_path: Path, capsys
) -> None:
    destination = tmp_path / "destination"
    state_directory = tmp_path / "state"
    log_file = tmp_path / "logs" / "sync.log"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "logging": {"file": str(log_file)},
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
                ],
            }
        ),
        encoding="utf-8",
    )

    class FakeService:
        def __init__(self) -> None:
            self.dry_runs: list[bool] = []
            self.closed = False

        def run(self, config, *, dry_run: bool = False) -> SyncSummary:
            self.dry_runs.append(dry_run)
            return SyncSummary(
                (
                    TargetSyncResult(
                        target_id="example",
                        status=TargetSyncStatus.CURRENT,
                        version="1.0",
                        change=None,
                        message="already current",
                    ),
                )
            )

        def close(self) -> None:
            self.closed = True

    service = FakeService()
    assert main(["--config", str(config_path)], service_factory=lambda logger: service) == 0
    assert service.dry_runs == [False]
    assert service.closed
    assert "Sync Summary" in capsys.readouterr().out
    assert log_file.exists()
    assert not destination.exists()
    assert not state_directory.exists()
