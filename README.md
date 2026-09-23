# nexus-jar-sync

`nexus-jar-sync` is intended to keep configured JAR artifacts synchronized from one or more Sonatype Nexus repositories. The current M0/M1 milestone provides the project foundation and validated, configuration-driven multi-target setup only. Nexus communication and downloading will be added in later milestones.

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

The command performs no network activity and creates no destination, state, or download files in this milestone.

Run the tests with:

```text
pytest
```
