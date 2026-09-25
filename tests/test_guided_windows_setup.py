from pathlib import Path

ROOT = Path(__file__).parents[1]
BATCH = ROOT / "deployment" / "offline" / "setup-windows.bat"
HELPER = ROOT / "deployment" / "offline" / "setup-windows.ps1"
BUILDER = ROOT / "deployment" / "offline" / "build_bundle.py"


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_batch_discovers_bundle_and_quotes_paths_with_spaces() -> None:
    value = text(BATCH)
    assert 'set "BUNDLE_ROOT=%~dp0"' in value
    assert '-File "%SETUP_HELPER%" -BundleRoot "%BUNDLE_ROOT%" %*' in value
    assert "DisableDelayedExpansion" in value
    assert "exit /b %SETUP_EXIT%" in value
    assert "-ExecutionPolicy" not in value and "Bypass" not in value


def test_user_profile_defaults_and_bundle_separation() -> None:
    value = text(HELPER)
    assert 'Join-Path $env:USERPROFILE "NexusJarSync"' in value
    assert '"venv-$($metadata.application_version)"' in value
    for relative in ("config\\config.yaml", 'Join-Path $install "data"', "test-downloads", "logs\\nexus-jar-sync.log"):
        assert relative in value
    assert "Installation root must be outside the extracted bundle" in value
    assert "$trimmed -eq $profile" in value


def test_manifest_and_compatibility_fail_before_installation() -> None:
    value = text(HELPER)
    install = value.index('Write-Phase "2 - Offline installation"')
    assert value.index('Write-Phase "1 - Bundle verification"') < value.index("manifest verification") < install
    assert value.index("Bundle platform does not match Windows") < install
    assert value.index("Python major/minor version does not match") < install
    assert value.index("7z.exe was not found") < install


def test_offline_install_is_strict_and_existing_environment_is_never_overwritten() -> None:
    value = text(HELPER)
    assert '"--no-index", "--find-links", $wheelhouse' in value
    assert "Existing environment preserved" in value
    assert "Validate and use this existing environment?" in value
    assert "Remove-Item" not in value
    assert "--index-url" not in value


def test_existing_configuration_is_preserved_and_credentials_are_status_only() -> None:
    value = text(HELPER)
    assert "Copy-Item -LiteralPath" in value
    assert "Existing configuration preserved" in value
    assert "Continue with this existing configuration?" in value
    assert 'Write-Host "$name : MISSING"' in value
    assert 'Write-Host "$name : SET"' in value
    assert "GetEnvironmentVariable" in value
    assert "Read-Host \"Password" not in value
    assert "Authorization" not in value


def test_mutating_phases_have_independent_gates_and_failure_boundaries() -> None:
    value = text(HELPER)
    gates = [
        "Start the isolated real test download?",
        "Have you inspected and approved the test output?",
        "Run one active production synchronization?",
        "Configure Task Scheduler now?",
        "Register the exact root task",
    ]
    positions = [value.index(gate) for gate in gates]
    assert positions == sorted(positions)
    test_call = value.index('"--test-download", "--test-output"')
    dry_run = value.index('"--dry-run"', test_call)
    active = value.index('"active production synchronization"')
    scheduler = value.index('"deployment\\windows\\install-task.ps1"')
    assert test_call < dry_run < active < scheduler
    assert value.count("Invoke-Checked") >= 7


def test_test_outputs_are_fresh_and_scheduler_is_never_forced() -> None:
    value = text(HELPER)
    assert "[Guid]::NewGuid()" in value
    assert "while (Test-Path -LiteralPath $candidate)" in value
    assert "Test output preserved at" in value
    scheduler = value[value.index('Write-Phase "7 - Optional Task Scheduler installation"'):]
    assert "-Force" not in scheduler
    assert "install-task.ps1" in scheduler


def test_sensitive_values_are_not_arguments_stdout_or_logs() -> None:
    value = text(HELPER)
    emitted = "\n".join(
        line for line in value.splitlines() if "Write-Host" in line or "Write-SafeLog" in line
    )
    assert "$present" not in emitted
    assert "$credentialJson" not in emitted
    assert "env $(cat" not in value


def test_offline_bundle_includes_both_guided_setup_files() -> None:
    value = text(BUILDER)
    assert 'offline / "setup-windows.bat"' in value
    assert 'offline / "setup-windows.ps1"' in value
