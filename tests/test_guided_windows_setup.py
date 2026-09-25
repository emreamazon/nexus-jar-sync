import json
import platform
from pathlib import Path
import shutil
import struct
import subprocess
import sys

import pytest

ROOT = Path(__file__).parents[1]
BATCH = ROOT / "deployment" / "offline" / "setup-windows.bat"
HELPER = ROOT / "deployment" / "offline" / "setup-windows.ps1"
BUILDER = ROOT / "deployment" / "offline" / "build_bundle.py"
SYNC_TEMPLATE = ROOT / "deployment" / "offline" / "sync-now.bat.template"


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
    for relative in ("config\\config.yaml", "Join-Path $install 'data'", "test-downloads", "logs\\nexus-jar-sync.log"):
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
    assert "'--no-index','--find-links',$wheelhouse" in value
    assert "Existing environment preserved" in value
    assert "Use this validated existing environment?" in value
    assert ".njs-setup-owned" in value
    assert "Remove-Item -LiteralPath $privateVenv" in value
    assert "--index-url" not in value


def test_existing_configuration_is_preserved_and_credentials_are_status_only() -> None:
    value = text(HELPER)
    assert "[IO.File]::Replace($configOutput,$config,$backup)" in value
    assert "Existing configuration preserved" in value
    assert "Back up and replace the existing configuration?" in value
    assert "existing configuration validation" in value
    assert "PROCESS=$(if($process){'SET'}else{'MISSING'})" in value
    assert "USER=$(if($user){'SET'}else{'MISSING'})" in value
    assert "MACHINE=$(if($machine){'SET'}else{'MISSING'})" in value
    assert "GetEnvironmentVariable" in value
    assert "Read-Host \"Password" not in value
    assert "Authorization" not in value


def test_mutating_phases_have_independent_gates_and_failure_boundaries() -> None:
    value = text(HELPER)
    gates = [
        "Start the isolated real test download?",
        "Have you inspected and approved the test output?",
        "Run one active production synchronization?",
    ]
    positions = [value.index(gate) for gate in gates]
    assert positions == sorted(positions)
    test_call = value.index("'--test-download','--test-output'")
    dry_run = value.index("'--dry-run'", test_call)
    active = value.index('"active production synchronization"')
    assert test_call < dry_run < active
    assert "install-task.ps1" not in value
    assert "Register-ScheduledTask" not in value
    assert value.count("Invoke-Checked") >= 7


def test_test_outputs_are_fresh_and_setup_never_accesses_scheduler() -> None:
    value = text(HELPER)
    assert "[Guid]::NewGuid()" in value
    assert "while(Test-Path -LiteralPath $c)" in value
    assert "Test output preserved at" in value
    assert "Task Scheduler" not in value
    assert "ScheduledTask" not in value


def test_sensitive_values_are_not_arguments_stdout_or_logs() -> None:
    value = text(HELPER)
    assert "GetEnvironmentVariable([string]$name,'Process')" in value
    assert "GetEnvironmentVariable([string]$name,'User')" in value
    assert "GetEnvironmentVariable([string]$name,'Machine')" in value
    assert "Write-Host $credentialJson" not in value
    assert "env $(cat" not in value


def test_offline_bundle_includes_both_guided_setup_files() -> None:
    value = text(BUILDER)
    assert 'offline / "setup-windows.bat"' in value
    assert 'offline / "setup-windows.ps1"' in value
    assert 'offline / "sync-now.bat.template"' in value


def test_manual_sync_template_is_one_shot_relative_and_preserves_exit_code() -> None:
    value = text(SYNC_TEMPLATE)
    assert 'set "INSTALL_ROOT=%~dp0"' in value
    assert 'set "PYTHON=%INSTALL_ROOT%__VENV_DIRECTORY__\\Scripts\\python.exe"' in value
    assert 'set "CONFIG=%INSTALL_ROOT%config\\config.yaml"' in value
    assert 'pushd "%INSTALL_ROOT%"' in value
    assert value.count('"%PYTHON%" -m nexus_jar_sync.main --config "%CONFIG%" --sanitized-errors') == 1
    assert 'set "SYNC_EXIT=%ERRORLEVEL%"' in value
    assert "pause >nul" in value
    assert "exit /b %SYNC_EXIT%" in value
    forbidden = ("-m pip", "ScheduledTask", "schtasks", "sc.exe", "while ", "timeout ", "sleep")
    assert not any(item.lower() in value.lower() for item in forbidden)


@pytest.mark.skipif(shutil.which("cmd.exe") is None, reason="Windows command processor unavailable")
def test_installed_manual_launcher_runs_once_from_space_and_unicode_path(tmp_path: Path) -> None:
    install = tmp_path / "Kurulum alanı & (test)"
    python_dir = install / "venv-1.0" / "Scripts"
    python_dir.mkdir(parents=True)
    marker = install / "observed.txt"
    fake_python = python_dir / "python.exe.cmd"
    fake_python.write_text(
        '@echo %CD%>>"%~dp0..\\..\\observed.txt"\n'
        '@echo %*>>"%~dp0..\\..\\observed.txt"\n'
        '@exit /b 7\n', encoding="ascii"
    )
    launcher = install / "sync-now.bat"
    content = text(SYNC_TEMPLATE).replace(
        "__VENV_DIRECTORY__\\Scripts\\python.exe",
        "venv-1.0\\Scripts\\python.exe.cmd",
    )
    launcher.write_text(content, encoding="ascii")
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "sync-now.bat"], cwd=install,
        input="x\n", text=True, capture_output=True, check=False,
    )
    assert result.returncode == 7
    observed = marker.read_text(encoding="utf-8")
    assert observed.count("nexus_jar_sync.main") == 1
    assert "-m nexus_jar_sync.main --config" in observed
    assert str(install) in observed


def _synthetic_bundle(tmp_path: Path, *, architecture: str | None = None, verifier_exit: int = 0) -> tuple[Path, Path]:
    bundle = tmp_path / "bundle with spaces"
    (bundle / "tools").mkdir(parents=True)
    (bundle / "SHA256SUMS.json").write_text("{}", encoding="utf-8")
    (bundle / "tools" / "verify_manifest.py").write_text(
        f"raise SystemExit({verifier_exit})\n", encoding="utf-8"
    )
    (bundle / "BUILD-METADATA.json").write_text(
        json.dumps({
            "application_version": "1.0.0", "source_commit": "abc123",
            "built_at_utc": "2026-01-01T00:00:00Z", "python_implementation": platform.python_implementation(),
            "python_version": platform.python_version(), "platform": "Windows",
            "architecture": architecture or platform.machine(),
        }), encoding="utf-8"
    )
    seven_zip = tmp_path / "Approved Tools" / "7-Zip" / "7z.cmd"
    seven_zip.parent.mkdir(parents=True)
    seven_zip.write_text("@exit /b 0\n", encoding="ascii")
    return bundle, seven_zip


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell unavailable")
def test_plan_only_executes_compatibility_and_custom_seven_zip_checks(tmp_path: Path) -> None:
    bundle, seven_zip = _synthetic_bundle(tmp_path)
    install = tmp_path / "install with spaces"
    result = subprocess.run([
        shutil.which("pwsh") or "pwsh", "-NoProfile", "-File", str(HELPER),
        "-BundleRoot", str(bundle), "-InstallationRoot", str(install),
        "-PythonExecutable", sys.executable, "-SevenZipExecutable", str(seven_zip), "-PlanOnly",
    ], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "PLAN OK" in result.stdout
    assert str(seven_zip) in result.stdout
    assert not install.exists()


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell unavailable")
@pytest.mark.parametrize("case", ["missing", "invalid", "architecture"])
def test_plan_only_stops_at_integrity_and_compatibility_boundaries(tmp_path: Path, case: str) -> None:
    mismatch = "arm64" if platform.machine().lower() in {"amd64", "x86_64", "x64"} else "amd64"
    bundle, seven_zip = _synthetic_bundle(
        tmp_path, architecture=mismatch if case == "architecture" else None,
        verifier_exit=1 if case == "invalid" else 0,
    )
    if case == "missing":
        (bundle / "SHA256SUMS.json").unlink()
    install = tmp_path / "install"
    result = subprocess.run([
        shutil.which("pwsh") or "pwsh", "-NoProfile", "-File", str(HELPER),
        "-BundleRoot", str(bundle), "-InstallationRoot", str(install),
        "-PythonExecutable", sys.executable, "-SevenZipExecutable", str(seven_zip), "-PlanOnly",
    ], text=True, capture_output=True, check=False)
    assert result.returncode != 0
    assert not install.exists()


@pytest.mark.skipif(shutil.which("cmd.exe") is None or shutil.which("powershell.exe") is None, reason="Windows shells unavailable")
def test_batch_wrapper_executes_from_space_path_forwards_arguments_and_exit_code(tmp_path: Path) -> None:
    bundle = tmp_path / "batch bundle with spaces"
    (bundle / "tools").mkdir(parents=True)
    shutil.copy2(BATCH, bundle / "setup-windows.bat")
    marker = bundle / "observed.txt"
    (bundle / "tools" / "setup-windows.ps1").write_text(
        "param([string]$BundleRoot,[string]$InstallationRoot)\n"
        f"Set-Content -LiteralPath '{str(marker).replace("'", "''")}' -Value ($BundleRoot + '|' + $InstallationRoot)\n"
        "exit 7\n", encoding="utf-8"
    )
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "setup-windows.bat", "-InstallationRoot", str(tmp_path / "install path")],
        cwd=bundle, text=True, capture_output=True, check=False,
    )
    if result.returncode != 7 and ("UnauthorizedAccess" in result.stderr or "SecurityError" in result.stderr):
        pytest.skip("Windows PowerShell execution policy blocked the batch helper")
    assert result.returncode == 7
    observed = marker.read_text(encoding="utf-8").strip()
    assert observed.split("|", 1)[0].rstrip("\\") == str(bundle)
    assert observed.split("|", 1)[1] == str(tmp_path / "install path")
