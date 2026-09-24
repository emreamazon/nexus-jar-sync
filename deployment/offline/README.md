# Offline deployment bundle

This guide prepares a bundle on a connected staging computer and installs it on a compatible offline computer. Build separately for each destination operating system, architecture, and compatible Python 3.12+ version. Dependency wheels are platform-sensitive; a Windows wheelhouse is not promised to work on Linux, or vice versa. The bundle does not include Python.

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

The examples show multiple independent targets, shared defaults, per-target credentials, classifier and main JARs, network overrides, retention, and disabled targets. Adding, removing, enabling, or changing targets requires only YAML changes. Define the referenced credential environment variables through the approved mechanism for the eventual scheduler account; never put values in YAML or the bundle.

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
