# nexus-jar-sync

`nexus-jar-sync` is intended to keep configured JAR artifacts synchronized from one or more Sonatype Nexus repositories. M0/M1 provide the project foundation and validated, configuration-driven multi-target setup. M2/M3 add reusable Nexus asset discovery with pagination and exact main/classifier filtering, plus independent per-target state storage and change detection.

M4/M5 add separately callable streamed downloads with checksum verification, same-directory temporary files and atomic deployment, plus target-specific local artifact retention.

M6/M7 add configurable rotating file logging, bounded fixed-delay retry for transient discovery and download failures, and a one-shot synchronization service. The service processes enabled targets sequentially, isolates expected per-target failures, and saves state only after download and retention succeed.

## Requirements and setup

Python 3.12 or newer is required.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -e .
```

On Linux, activate the environment with `source .venv/bin/activate`; the remaining `python` commands are the same.

Copy `config/config.example.yaml` to `config/config.yaml`, then edit its placeholder Nexus coordinates and destination paths. Add future artifacts by adding target entries to YAML—Python source changes are not required.

Credentials are referenced by environment-variable name in YAML and resolved when configuration loads. Never write credential values into the YAML file. For the example configuration, define:

```powershell
$env:NEXUS_USERNAME = "your-username"
$env:NEXUS_PASSWORD = "your-password"
$env:LIBRARY_NEXUS_USERNAME = "other-username"
$env:LIBRARY_NEXUS_PASSWORD = "other-password"
```

```bash
export NEXUS_USERNAME="your-username"
export NEXUS_PASSWORD="your-password"
export LIBRARY_NEXUS_USERNAME="other-username"
export LIBRARY_NEXUS_PASSWORD="other-password"
```

## Run

Validate configuration and list enabled targets:

```text
nexus-jar-sync --config config/config.yaml
```

The command still performs configuration validation only: it performs no network activity, retry, synchronization, or logging setup and creates no destination, state, log, or download files. The synchronization service is available as a library API but is not yet connected to the CLI.

Active CLI synchronization and `--dry-run` belong to M8. Scheduling remains external and is not implemented by this project.

Run the tests with:

```text
pytest
```
