from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
WINDOWS = ROOT / "deployment" / "windows"
LINUX = ROOT / "deployment" / "linux"
INSTALL = WINDOWS / "install-task.ps1"
UNINSTALL = WINDOWS / "uninstall-task.ps1"
SERVICE = LINUX / "nexus-jar-sync.service.example"
TIMER = LINUX / "nexus-jar-sync.timer.example"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_scheduling_artifacts_exist() -> None:
    for path in (INSTALL, UNINSTALL, WINDOWS / "README.md", SERVICE, TIMER, LINUX / "README.md"):
        assert path.is_file(), path


def test_windows_installer_exposes_and_validates_required_parameters() -> None:
    script = read(INSTALL)
    for parameter in (
        "ProjectDirectory",
        "ConfigPath",
        "PythonExecutable",
        "IntervalMinutes",
        "TaskName",
    ):
        assert f"${parameter}" in script
    assert "[ValidateRange(1," in script
    assert "[string]::IsNullOrWhiteSpace($TaskName)" in script
    assert "Test-Path -LiteralPath" in script
    assert "Resolve-Path -LiteralPath" in script
    assert "IsPathFullyQualified" in script
    assert script.index("Resolve-RequiredPath -LiteralPath $PythonExecutable") < script.index(
        "Get-ScheduledTask -TaskName"
    )


def test_windows_action_is_explicit_one_shot_with_working_directory() -> None:
    script = read(INSTALL)
    assert "New-ScheduledTaskAction" in script
    assert "-Execute $resolvedPython" in script
    assert "-m nexus_jar_sync.main --config" in script
    assert "--config \"{0}\"' -f $resolvedConfig" in script
    assert "-WorkingDirectory $resolvedProject" in script
    assert "cmd.exe" not in script.lower()
    assert "while ($true)" not in script.lower()


def test_windows_trigger_and_settings_are_headless_safe() -> None:
    script = read(INSTALL)
    assert "New-ScheduledTaskTrigger" in script
    assert "-RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)" in script
    assert "-StartWhenAvailable" in script
    assert "-MultipleInstances IgnoreNew" in script


def test_windows_replacement_requires_force_and_is_exact_name_only() -> None:
    script = read(INSTALL)
    assert "$existingTask" in script
    assert "-not $Force" in script
    assert "already exists" in script
    assert "-TaskName $TaskName" in script
    assert "-Force:$Force" in script
    assert "-TaskName *" not in script


def test_windows_credentials_are_not_embedded_or_plaintext_parameters() -> None:
    combined = read(INSTALL) + read(UNINSTALL)
    assert "[System.Management.Automation.PSCredential]$TaskCredential" in combined
    assert not re.search(r"\[string\]\s*\$(?:Password|NexusPassword)", combined, re.IGNORECASE)
    assert "NEXUS_PASSWORD=" not in combined
    assert "username_env:" not in combined
    assert "password_env:" not in combined
    assert "C:\\Users\\" not in combined


def test_windows_uninstall_is_exact_idempotent_and_non_destructive() -> None:
    script = read(UNINSTALL)
    assert "SupportsShouldProcess = $true" in script
    assert "Get-ScheduledTask -TaskName $TaskName" in script
    assert "Unregister-ScheduledTask -TaskName $TaskName" in script
    assert "nothing to remove" in script
    assert "Remove-Item" not in script
    assert "-TaskName *" not in script
    for forbidden in ("config.yaml", ".venv", "data\\state", "logs\\"):
        assert forbidden not in script


@pytest.mark.parametrize("script_path", [INSTALL, UNINSTALL])
def test_powershell_scripts_parse_when_powershell_is_available(script_path: Path) -> None:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        pytest.skip("PowerShell is unavailable")
    quoted_path = str(script_path).replace("'", "''")
    parser = (
        "$tokens=$null;$errors=$null;"
        f"[System.Management.Automation.Language.Parser]::ParseFile('{quoted_path}',"
        "[ref]$tokens,[ref]$errors)|Out-Null;"
        "if($errors.Count -ne 0){$errors|ForEach-Object{Write-Error $_};exit 1}"
    )
    completed = subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-Command", parser],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_linux_service_is_explicit_one_shot() -> None:
    service = read(SERVICE)
    assert "Type=oneshot" in service
    assert "User=nexus-jar-sync" in service
    assert "Group=nexus-jar-sync" in service
    assert "WorkingDirectory=/opt/nexus-jar-sync" in service
    assert (
        "ExecStart=/opt/nexus-jar-sync/.venv/bin/python -m nexus_jar_sync.main "
        "--config /etc/nexus-jar-sync/config.yaml"
    ) in service
    assert "EnvironmentFile=/etc/nexus-jar-sync/credentials.env" in service
    assert "Restart=always" not in service
    assert "NoNewPrivileges=true" in service
    assert "PrivateTmp=true" in service
    assert "C:\\Users\\" not in service


def test_linux_timer_is_periodic_persistent_and_non_overlapping_by_systemd() -> None:
    timer = read(TIMER)
    assert "OnBootSec=" in timer
    assert "OnUnitActiveSec=" in timer
    assert "Persistent=true" in timer
    assert "Unit=nexus-jar-sync.service" in timer
    assert "WantedBy=timers.target" in timer


def test_linux_artifacts_contain_no_embedded_credentials() -> None:
    combined = read(SERVICE) + read(TIMER)
    for forbidden in ("NEXUS_USERNAME=", "NEXUS_PASSWORD=", "password=", "real-user"):
        assert forbidden.lower() not in combined.lower()


def test_platform_documentation_covers_operations_permissions_and_placeholders() -> None:
    windows = read(WINDOWS / "README.md")
    linux = read(LINUX / "README.md")
    for phrase in (
        "--dry-run",
        "Get-ScheduledTaskInfo",
        "Start-ScheduledTask",
        "LastTaskResult",
        "working directory",
        "-Force",
        "logged on or not",
    ):
        assert phrase.lower() in windows.lower()
    for phrase in (
        "--dry-run",
        "0600",
        "systemd-analyze verify",
        "systemctl enable --now",
        "systemctl list-timers",
        "journalctl -u",
        "placeholders",
        "WorkingDirectory",
    ):
        assert phrase.lower() in linux.lower()


def test_no_scheduler_dependency_or_application_loop_was_added() -> None:
    dependencies = read(ROOT / "requirements.txt") + read(ROOT / "pyproject.toml")
    for library in ("apscheduler", "schedule", "celery"):
        assert library not in dependencies.lower()
    source = "\n".join(
        read(ROOT / "src" / "nexus_jar_sync" / name)
        for name in ("main.py", "sync.py")
    )
    assert "while True" not in source
    assert "systemctl" not in source
    assert "Register-ScheduledTask" not in source
