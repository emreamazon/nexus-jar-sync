# Offline deployment bundle

This guide prepares a bundle on a connected staging computer and installs it on a compatible offline computer. Build separately for each destination operating system, architecture, and compatible Python 3.12+ version. Dependency wheels are platform-sensitive; a Windows wheelhouse is not promised to work on Linux, or vice versa. The bundle does not include Python.

The bundle also does not include 7-Zip. Install an organization-approved compatible 7-Zip separately on the offline destination and set the exact executable path in `tools.seven_zip_executable` before using any `extract_7z` companion.

The SHA-256 manifest detects missing, additional, or modified files after generation. It is not a signature and does not authenticate an untrusted source.

## Build on the connected staging computer

Start from the reviewed, clean source revision and use a Python environment with pip and wheel-building support:

```powershell
git status --short
python deployment/offline/build_bundle.py --output-directory C:\BundleOutput
```

```bash
git status --short
python3 deployment/offline/build_bundle.py --output-directory /var/tmp/nexus-bundles
```

The builder creates a fresh private stage, builds the application wheel, downloads binary runtime dependency wheels with `--only-binary=:all:`, writes provenance and a sorted manifest, and produces a ZIP plus an archive `.sha256` sidecar. It refuses a dirty checkout or an existing output filename and never merges a stale wheelhouse.

Record the reported archive checksum through an organization-approved channel. Verify the archive hash before and after transfer (`Get-FileHash -Algorithm SHA256` on Windows or `sha256sum` on Linux), extract it into a new directory, then verify every extracted file:

```powershell
Get-FileHash -Algorithm SHA256 .\nexus-jar-sync-*.zip
Expand-Archive .\nexus-jar-sync-*.zip -DestinationPath C:\NexusJarSyncBundle
python C:\NexusJarSyncBundle\tools\verify_manifest.py C:\NexusJarSyncBundle
```

```bash
sha256sum nexus-jar-sync-*.zip
unzip nexus-jar-sync-*.zip -d /opt/nexus-jar-sync-bundle
python3 /opt/nexus-jar-sync-bundle/tools/verify_manifest.py /opt/nexus-jar-sync-bundle
```

Transfer only through the organization-approved mechanism. The archive contains no populated `config.yaml`, credential file, virtual environment, logs, state, downloaded JAR, Python interpreter, Git metadata, or developer cache.

## Install without network access

Confirm that the offline host matches the staging host’s OS/architecture and has Python 3.12 or newer. Create a new environment and install strictly from `wheelhouse`; no index fallback is allowed.

Windows helper:

```powershell
cd C:\NexusJarSyncBundle
.\install-offline.ps1 -EnvironmentDirectory C:\NexusJarSync\venv-0.1.0 -PythonExecutable python
```

For a guided per-user Windows deployment, extract the bundle beneath your user profile and double-click `setup-windows.bat`. It verifies the manifest plus the selected Python implementation, major/minor version, bitness, and normalized machine architecture before making changes. It preserves existing environments/configuration/data, reports credential-variable readiness separately for process, user, and machine scopes without values, and gates test-download, dry-run, active synchronization, and Task Scheduler registration behind separate confirmations. Defaults are `%USERPROFILE%\NexusJarSync`, `venv-<version>`, `config\config.yaml`, `data`, `logs`, and `test-downloads`; the extracted bundle remains read-only source material. Run `setup-windows.bat -InstallationRoot "D:\Reviewed Path" -PythonExecutable "C:\Path To\python.exe" -SevenZipExecutable "D:\Approved Tools\7-Zip\7z.exe"` for explicit overrides. 7-Zip resolution checks the override, an existing reviewed config, Program Files, then PATH, and executes a harmless capability check. The wrapper does not bypass PowerShell execution policy; if policy blocks the reviewed helper, contact your administrator.

The guided installer builds a new virtual environment privately and publishes it only after offline installation, entry-point, and help checks succeed. It never overwrites an existing environment or config. Scheduler setup explicitly offers a current-user logged-on-only task, a credential-backed headless task using a secure `Get-Credential` prompt, or deferral. A different headless account's environment and permissions cannot be validated by the setup user's preflight; verify that account's Nexus network access, credential variables, config/CA reads, destination/state/log writes, and 7-Zip access separately.

Linux helper:

```bash
cd /opt/nexus-jar-sync-bundle
./install-offline.sh /opt/nexus-jar-sync/venv-0.1.0 python3
```

Equivalent manual installation is:

```text
python -m pip install --no-index --find-links wheelhouse nexus-jar-sync
```

The helpers require a new environment directory and never create credentials, copy configuration, register a scheduler, or run synchronization. Missing or incompatible wheels make pip fail clearly without network fallback.

## Configure and verify

Copy, never overwrite, the appropriate safe example and edit YAML only:

```powershell
$configPath = "C:\NexusJarSync\config.yaml"
if (Test-Path -LiteralPath $configPath) { throw "Configuration already exists: $configPath" }
Copy-Item -LiteralPath .\config\config.windows.example.yaml -Destination $configPath
```

```bash
cp --no-clobber config/config.linux.example.yaml /etc/nexus-jar-sync/config.yaml
```

The examples show a primary `windows-versions` Maven JAR, three exact-version secondary JARs, fixed `dependencies.7z` and `license.txt` companions, plus a second independent target. `destination.directory` is the base; the primary version is the only release trigger and creates an append-only child directory. Every secondary is resolved at that exact version and the complete release publishes atomically. Dependencies demonstrate `strip_single_root: true` and discard only the privately staged archive after successful extraction. Companion or secondary changes do not refresh a completed release. Old releases are never removed, and flat-layout artifacts are not migrated.

The guided Windows setup defaults to anonymous Nexus access and generates YAML from separately validated argument values; optional authentication records environment-variable names only. Existing configuration is validated and preserved unless the operator explicitly requests a timestamped backup and replacement. HTTP must be explicitly acknowledged and provides no transport integrity; prefer HTTPS and configure a CA bundle when required. Existing earlier-bundle installations should retain releases, state, logs, and configuration, install the new bundle into a new versioned environment, validate or deliberately replace configuration, run test-download/dry-run/one active pass, then update the five-minute external scheduled task.

Nexus access is GET-only. Give the runtime identity only the browse/read permissions required for its configured repositories.

Run these manually before scheduling:

```powershell
C:\NexusJarSync\venv-0.1.0\Scripts\nexus-jar-sync.exe --help
C:\NexusJarSync\venv-0.1.0\Scripts\python.exe -m nexus_jar_sync.main --config C:\NexusJarSync\config.yaml --dry-run
C:\NexusJarSync\venv-0.1.0\Scripts\python.exe -m nexus_jar_sync.main --config C:\NexusJarSync\config.yaml
```

```bash
/opt/nexus-jar-sync/venv-0.1.0/bin/nexus-jar-sync --help
/opt/nexus-jar-sync/venv-0.1.0/bin/python -m nexus_jar_sync.main --config /etc/nexus-jar-sync/config.yaml --dry-run
/opt/nexus-jar-sync/venv-0.1.0/bin/python -m nexus_jar_sync.main --config /etc/nexus-jar-sync/config.yaml
```

Inspect the summary, configured logs, state, and destination JARs. Only after these checks, follow the bundled Windows Task Scheduler or Linux systemd instructions. Review scheduler paths whenever the release or environment changes.

## Upgrade and rollback

Copying a newer bundle does not change the active installation. Build and verify a new compatible bundle, install it into a new virtual environment or another recoverable release directory, retain operator-managed configuration, credentials, logs, state, and JARs, rerun manual verification, then deliberately update scheduler paths. Do not copy `.venv` directories between computers.

Rollback means pointing execution back to the prior intact environment/package while retaining operational data, subject to organizational policy. Do not overwrite operational data as part of a package upgrade.
