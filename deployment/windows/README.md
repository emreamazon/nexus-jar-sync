# Windows manual operation and optional advanced scheduling

The recommended Windows workflow is the guided offline installer followed by double-clicking `sync-now.bat` for each desired synchronization. Every invocation performs one pass and exits; overlapping production runs for the same state directory are rejected by a process-lifetime lock. Guided setup does not create or access Task Scheduler, a service, or a background process.

The scripts below are retained only for backward-compatible advanced deployments. They are not offered or invoked by the default setup. Use them only after a deliberate operational decision to adopt OS scheduling.

These scripts register the existing one-shot Python command. They do not install Python, the package, credentials, or application data, and they do not add an application polling loop.

## Prepare and verify

1. Create the environment and install the project:

   ```powershell
   cd C:\NexusJarSync
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   python -m pip install .
   Copy-Item config\config.example.yaml config\config.yaml
   ```

2. Edit `config\config.yaml`. Relative log, state, CA-bundle, and destination paths resolve from the scheduled task working directory (`ProjectDirectory`). Review that directory deliberately.
3. Define every environment variable referenced by `username_env` or `password_env` for the Windows account that will run the task. Persistent user-level variables for that account, deliberately administered machine-level variables, or an organization-approved secret-injection mechanism are suitable. `$env:NEXUS_PASSWORD = "..."` affects only the current process and is not available to a later scheduled task. Do not commit a `.env` file or place Nexus credentials in YAML, task arguments, descriptions, scripts, or logs.
4. As the intended task account, run both preflight checks:

   ```powershell
   C:\NexusJarSync\.venv\Scripts\python.exe -m nexus_jar_sync.main --config C:\NexusJarSync\config\config.yaml --dry-run
   C:\NexusJarSync\.venv\Scripts\python.exe -m nexus_jar_sync.main --config C:\NexusJarSync\config\config.yaml
   ```

These checks validate configuration, credential visibility, Nexus/network/TLS access, CA configuration, and destination, state, and log permissions. The installer never runs synchronization automatically.

## Choose the task identity

Without `-TaskCredential`, the installer uses the current Windows identity with an interactive-token logon, so the task runs only while that user is logged on. For headless “run whether the user is logged on or not” operation, pass a `PSCredential`; PowerShell can prompt securely:

```powershell
$taskAccount = Get-Credential -Message "Windows account for NexusJarSync"
```

The script converts that secure credential only in memory because the ScheduledTasks API requires it during registration. It never prints it, writes it to a file, or places it in task arguments. This Windows account password is separate from Nexus credentials. Do not supply plaintext passwords on a command line. The template does not default to S4U because network authentication can be unavailable, and it does not default to SYSTEM because that identity needs separately administered credentials and filesystem permissions.

The chosen account needs GET-only Nexus browse/read access and read/write access to configured destination bases, state, and logs, plus read access to the config and any CA bundle. Each exact Nexus version becomes an append-only child directory of its destination base; old artifacts are not cleaned up. Credential environment variables must be visible to that same account.

For `extract_7z` companions, install 7-Zip for the task account and configure its exact executable path. Before scheduling, run an isolated real download such as `nexus-jar-sync --config C:\NexusJarSync\config\config.yaml --test-download --test-output C:\NexusJarSyncTest\run-001`; this performs network reads and extraction but does not touch production state or destinations.

The supplied guided offline setup generates a primary-triggered four-JAR configuration, with secondaries resolved at the primary version and dependencies safely flattened from one wrapper directory. Anonymous Nexus read access is the default; credentials are optional environment-variable references. HTTP endpoints require explicit operator acceptance and should be limited to trusted internal networks because HTTP has no transport integrity.

## Install and operate

Run from an appropriately privileged PowerShell session:

```powershell
.\deployment\windows\install-task.ps1 `
  -ProjectDirectory "C:\NexusJarSync" `
  -ConfigPath "C:\NexusJarSync\config\config.yaml" `
  -PythonExecutable "C:\NexusJarSync\.venv\Scripts\python.exe" `
  -IntervalMinutes 5 `
  -TaskName "NexusJarSync" `
  -TaskCredential $taskAccount
```

All paths are resolved and validated as absolute paths before scheduler registration. Task names must be plain names: wildcard characters, `/`, `\`, and control characters are rejected. Tasks are always selected and registered in the root Task Scheduler folder (`\`), so an identically named task in another folder is untouched. An existing exact root-folder task causes installation to fail without changes; use `-Force` to replace only that exact task. The task invokes `python -m nexus_jar_sync.main --config ...`, starts missed runs when the machine becomes available, and uses Task Scheduler’s `IgnoreNew` policy to prevent overlap.

Inspect and test it:

```powershell
Get-ScheduledTask -TaskPath "\" -TaskName "NexusJarSync"
Get-ScheduledTaskInfo -TaskPath "\" -TaskName "NexusJarSync"
Start-ScheduledTask -TaskPath "\" -TaskName "NexusJarSync"
Get-ScheduledTaskInfo -TaskPath "\" -TaskName "NexusJarSync"
Get-Content C:\NexusJarSync\logs\nexus-jar-sync.log -Tail 100
```

`Start-ScheduledTask` queues the run; it does not synchronously return the application exit code. Inspect `LastTaskResult` after completion. Application exit code `0` means a completed run with no failed targets, `1` means one or more target failures, and `2` means a CLI/configuration/startup error. The process exit code is preserved as the task’s last result.

## Remove

```powershell
.\deployment\windows\uninstall-task.ps1 -TaskName "NexusJarSync"
```

The removal resolves the exact task in the root folder, unregisters that resolved object, is idempotent, and supports `-WhatIf`/`-Confirm`. It does not remove same-named tasks in other folders, the project, virtual environment, configuration, logs, state, JARs, credentials, or environment variables.
