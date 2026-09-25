from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import nexus_jar_sync.main as main_module
from nexus_jar_sync.main import main
from nexus_jar_sync.state import ChangeDecision
from nexus_jar_sync.sync import SyncSummary, TargetSyncResult, TargetSyncStatus


def write_config(
    tmp_path: Path, *, username_env: str | None = None, password_env: str | None = None
) -> Path:
    target: dict[str, object] = {
        "id": "example",
        "nexus": {
            "url": "https://nexus.example.com",
            "repository": "releases",
            "group_id": "com.example",
            "artifact_id": "application",
        },
        "destination": {"directory": str(tmp_path / "destination")},
    }
    if username_env is not None or password_env is not None:
        target["auth"] = {
            "username_env": username_env,
            "password_env": password_env,
        }
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "logging": {"file": str(tmp_path / "logs" / "sync.log")},
                "state": {"directory": str(tmp_path / "state")},
                "targets": [target],
            }
        ),
        encoding="utf-8",
    )
    return path


class FakeService:
    def __init__(self, summary: SyncSummary | BaseException) -> None:
        self.summary = summary
        self.dry_runs: list[bool] = []
        self.closed = False

    def run(self, config, *, dry_run: bool = False) -> SyncSummary:
        self.dry_runs.append(dry_run)
        if isinstance(self.summary, BaseException):
            raise self.summary
        return self.summary

    def close(self) -> None:
        self.closed = True


def result(target: str, status: TargetSyncStatus) -> TargetSyncResult:
    return TargetSyncResult(
        target_id=target,
        status=status,
        version="2.0" if status is not TargetSyncStatus.FAILED else None,
        change=ChangeDecision.VERSION_CHANGED if status in {TargetSyncStatus.UPDATED, TargetSyncStatus.WOULD_UPDATE} else None,
        message="sanitized failure" if status is TargetSyncStatus.FAILED else "done",
    )


@pytest.mark.parametrize(
    ("statuses", "dry_run", "exit_code"),
    [
        ((), False, 0),
        ((TargetSyncStatus.UPDATED, TargetSyncStatus.CURRENT), False, 0),
        ((TargetSyncStatus.WOULD_UPDATE, TargetSyncStatus.CURRENT), True, 0),
        ((TargetSyncStatus.CURRENT, TargetSyncStatus.FAILED), False, 1),
        ((TargetSyncStatus.WOULD_UPDATE, TargetSyncStatus.FAILED), True, 1),
    ],
)
def test_cli_exit_codes_summary_order_and_dry_run(
    tmp_path: Path, capsys, statuses, dry_run: bool, exit_code: int
) -> None:
    summary = SyncSummary(tuple(result(f"target-{index}", status) for index, status in enumerate(statuses)))
    service = FakeService(summary)
    arguments = ["--config", str(write_config(tmp_path))]
    if dry_run:
        arguments.append("--dry-run")
    assert main(arguments, service_factory=lambda logger: service) == exit_code
    output = capsys.readouterr().out
    assert service.dry_runs == [dry_run]
    assert service.closed
    assert output.startswith("Dry-Run Summary" if dry_run else "Sync Summary")
    positions = [output.index(f"target-{index}") for index in range(len(statuses))]
    assert positions == sorted(positions)
    assert f"Failed: {sum(status is TargetSyncStatus.FAILED for status in statuses)}" in output
    assert ("No artifact, destination, or state changes were made." in output) is dry_run


def test_invalid_configuration_exits_two_before_logging_or_service(tmp_path: Path, capsys) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("targets: nope", encoding="utf-8")
    called = False
    def factory(logger):
        nonlocal called
        called = True
        raise AssertionError
    assert main(["--config", str(path)], service_factory=factory) == 2
    captured = capsys.readouterr()
    assert "Configuration error:" in captured.err
    assert "Traceback" not in captured.err
    assert not called


@pytest.mark.parametrize("contents", [None, "targets: ["])
def test_missing_file_and_invalid_yaml_exit_two_without_traceback(
    tmp_path: Path, capsys, contents: str | None
) -> None:
    path = tmp_path / "config.yaml"
    if contents is not None:
        path.write_text(contents, encoding="utf-8")
    assert main(["--config", str(path)], service_factory=lambda logger: pytest.fail()) == 2
    captured = capsys.readouterr()
    assert "Configuration error:" in captured.err
    assert "Traceback" not in captured.err
    assert not (tmp_path / "logs").exists()


def test_logging_initialization_failure_exits_two_before_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(main_module, "configure_logging", lambda config: (_ for _ in ()).throw(OSError()))
    assert main(["--config", str(write_config(tmp_path))], service_factory=lambda logger: pytest.fail()) == 2
    captured = capsys.readouterr()
    assert captured.err == "Logging initialization failed.\n"
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("error", [RuntimeError("bug"), KeyboardInterrupt()])
def test_unexpected_errors_propagate_and_resources_close(tmp_path: Path, error: BaseException) -> None:
    service = FakeService(error)
    with pytest.raises(type(error)):
        main(["--config", str(write_config(tmp_path))], service_factory=lambda logger: service)
    assert service.closed


def test_summary_and_errors_do_not_expose_resolved_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    username, password = "private-user", "private-password"
    monkeypatch.setenv("TEST_NEXUS_USERNAME", username)
    monkeypatch.setenv("TEST_NEXUS_PASSWORD", password)
    service = FakeService(SyncSummary((result("example", TargetSyncStatus.FAILED),)))
    assert main(
        [
            "--config",
            str(
                write_config(
                    tmp_path,
                    username_env="TEST_NEXUS_USERNAME",
                    password_env="TEST_NEXUS_PASSWORD",
                )
            ),
        ],
        service_factory=lambda logger: service,
    ) == 1
    captured = capsys.readouterr()
    assert username not in captured.out + captured.err
    assert password not in captured.out + captured.err
    log_text = (tmp_path / "logs" / "sync.log").read_text(encoding="utf-8")
    assert username not in log_text
    assert password not in log_text


@pytest.mark.parametrize("field", ["username", "password", "user_env"])
def test_direct_or_unknown_auth_field_exits_two_before_logging_and_service(
    tmp_path: Path, field: str, capsys
) -> None:
    secret = "direct-secret-value"
    path = write_config(tmp_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["targets"][0]["auth"] = {field: secret}
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    called = False

    def factory(logger):
        nonlocal called
        called = True
        raise AssertionError

    assert main(["--config", str(path)], service_factory=factory) == 2
    captured = capsys.readouterr()
    assert field in captured.err
    assert secret not in captured.err
    assert "Traceback" not in captured.err
    assert not called
    assert not (tmp_path / "logs").exists()


def test_failed_summary_displays_preserved_version(tmp_path: Path, capsys) -> None:
    failed = TargetSyncResult(
        target_id="example",
        status=TargetSyncStatus.FAILED,
        version="4.2.0",
        change=ChangeDecision.VERSION_CHANGED,
        message="sanitized failure",
    )
    service = FakeService(SyncSummary((failed,)))
    assert main(
        ["--config", str(write_config(tmp_path))],
        service_factory=lambda logger: service,
    ) == 1
    assert "example  FAILED  4.2.0  version_changed" in capsys.readouterr().out


def test_console_script_metadata_points_to_main() -> None:
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    assert 'nexus-jar-sync = "nexus_jar_sync.main:main"' in pyproject.read_text(encoding="utf-8")


def test_help_and_missing_config_are_argparse_errors(capsys) -> None:
    with pytest.raises(SystemExit) as help_exit:
        main(["--help"])
    assert help_exit.value.code == 0
    assert "--dry-run" in capsys.readouterr().out


@pytest.mark.parametrize(
    "arguments",
    [
        ["--test-download"],
        ["--test-output", "C:/test-output"],
        ["--test-download", "--test-output", "C:/test-output", "--dry-run"],
    ],
)
def test_test_download_option_combinations_are_rejected(
    tmp_path: Path, arguments: list[str]
) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["--config", str(write_config(tmp_path)), *arguments])
    assert caught.value.code == 2


def test_test_download_requires_absolute_fresh_nonproduction_output(tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    for output in ("relative", str(tmp_path), str(tmp_path / "destination")):
        assert main(["--config", str(config_path), "--test-download", "--test-output", output]) == 2


def test_test_download_uses_isolated_service_path_and_reports_network_notice(
    tmp_path: Path, capsys
) -> None:
    class TestService(FakeService):
        def __init__(self) -> None:
            super().__init__(SyncSummary((result("one", TargetSyncStatus.UPDATED),)))
            self.output: Path | None = None

        def run_test_download(self, config, output_root: Path) -> SyncSummary:
            self.output = output_root
            return self.summary  # type: ignore[return-value]

    service = TestService()
    output = tmp_path / "isolated-output"
    code = main(
        ["--config", str(write_config(tmp_path)), "--test-download", "--test-output", str(output.resolve())],
        service_factory=lambda logger: service,
    )
    assert code == 0
    assert service.output == output.resolve()
    assert output.is_dir()
    assert "real network reads and downloads" in capsys.readouterr().out
    with pytest.raises(SystemExit) as missing_exit:
        main([])
    assert missing_exit.value.code == 2
