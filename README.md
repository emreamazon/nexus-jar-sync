# nexus-jar-sync

`nexus-jar-sync` is intended to keep configured JAR artifacts synchronized from one or more Sonatype Nexus repositories. M0/M1 provide the project foundation and validated, configuration-driven multi-target setup. M2/M3 add reusable Nexus asset discovery with pagination and exact main/classifier filtering, plus independent per-target state storage and change detection.

M4/M5 added streamed, checksum-verified downloads. M12 changes deployed artifact storage to append-only version directories and retires automatic retention.

M6/M7 add configurable rotating file logging, bounded fixed-delay retry for transient discovery and download failures, and a one-shot synchronization service. M8/M9 connect that service to the CLI, including a read-only dry-run mode. M10 supplies operating-system scheduling examples while keeping the application one-shot.

M13 models a release as one version-discovered primary Maven artifact plus explicitly configured fixed-URL companions. The primary version is the sole release trigger.

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

Run one synchronization pass over all enabled targets:

```text
nexus-jar-sync --config config/config.yaml
```

`destination.directory` is a base directory. Each target publishes a completed release at `<base>/<exact validated primary version>/`. The primary Maven artifact and that target's fixed companions live together there. Separate independently versioned applications remain separate targets.

Completed releases and version directories are append-only: old versions are never deleted, refreshed, or overwritten. Each new primary version downloads fresh copies of all configured companions. Companion content, timestamps, and headers never trigger a release independently. A same-version primary checksum conflict fails and requires operator investigation. Existing artifacts in the former flat layout are left untouched and are not migrated automatically. Only owned staging content, atomically replaced state files, and rotating logs are operational exceptions to artifact immutability. Disk usage therefore grows and must be monitored externally.

Companions use explicit direct HTTP/HTTPS URLs and either `copy` or `extract_7z`. Every required fixed file must be declared; Maven dependency resolution is not performed. `extract_7z` requires an external 7-Zip executable configured with `tools.seven_zip_executable`. Archives are listed before extraction, extracted without a shell into private staging, inspected again on disk, and rejected for traversal, links, reparse points, unsafe names, or collisions.

Nexus discovery and downloads are strictly HTTP GET-only. The application never uploads, updates, or deletes Nexus content and does not require repository write permissions. Use a least-privilege Nexus identity with only the browse/read permissions needed for configured repositories.

Preview discovery and change decisions without downloading, writing state, or creating destination files:

```text
nexus-jar-sync --config config/config.yaml --dry-run
```

Dry-run reads Nexus metadata and existing state, and still performs bounded discovery retries. It does not create JARs, version or state directories, or save state. Configured logging may create or append to its log file.

Both modes print a deterministic per-target summary. Exit code `0` means all enabled targets completed successfully (including targets that would update during a dry run), `1` means at least one target failed, and `2` means configuration loading or logging initialization failed. Unexpected programming errors and interrupts are not hidden.

To exercise the complete current release workflow without touching production destinations or state, use a new absolute directory:

```text
nexus-jar-sync --config config/config.yaml --test-download --test-output C:/NexusJarSyncTest/run-001
```

This is not a dry-run: it performs real Nexus GET discovery/downloads and real 7-Zip extraction. It never writes Nexus, production state, or configured production destinations. The output must not already exist and cannot overlap protected paths. Inspect the isolated releases before active deployment. `--test-download` and `--dry-run` are mutually exclusive.

## Headless scheduling

The application always performs one synchronization pass and exits. Scheduling frequency belongs to the operating system; no polling loop or scheduler runs inside Python.

- [Windows Task Scheduler instructions](deployment/windows/README.md)
- [Linux systemd instructions](deployment/linux/README.md)

Run one manual dry-run and one manual active run as the intended scheduler account before installation. That account must be able to read its configured credential environment variables and access Nexus, configuration, destinations, state, logs, and any CA bundle. The supplied examples set an explicit working directory and prevent overlapping invocations.

Headless scheduling is supplied by M10; portable/offline deployment packaging is supplied by M11.

## Offline deployment

M11 provides a platform-specific wheelhouse bundle builder, integrity manifest, strict offline installers, and aligned Windows/Linux multi-JAR examples. See the [offline deployment guide](deployment/offline/README.md). Build bundles on a connected host compatible with the offline destination; Python 3.12+ must already be installed on the destination.

Run the tests with:

```text
pytest
```
