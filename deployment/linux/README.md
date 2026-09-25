# Linux systemd deployment

The example units run the existing command once per timer activation. Copy and edit them before installation; `/opt/nexus-jar-sync`, `/etc/nexus-jar-sync`, and the `nexus-jar-sync` account are placeholders requiring deployment-specific review.

## Prepare and verify

1. Create a dedicated service account where appropriate, then install the project and environment under `/opt/nexus-jar-sync`:

   ```bash
   sudo useradd --system --home /opt/nexus-jar-sync --shell /usr/sbin/nologin nexus-jar-sync
   cd /opt/nexus-jar-sync
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   .venv/bin/python -m pip install .
   ```

2. Place the reviewed configuration at `/etc/nexus-jar-sync/config.yaml` (or update the unit). Relative logging, state, CA-bundle, and destination paths resolve from `WorkingDirectory=/opt/nexus-jar-sync`.
3. Create `/etc/nexus-jar-sync/credentials.env` outside the Git repository with only the environment values referenced by YAML. Do not include real values in unit files or documentation. Restrict the file, for example with root ownership and mode `0600`, while ensuring systemd can load it for the service. Never commit it, print it in diagnostics, or copy it into issue reports. An organization-approved secret manager or systemd credentials can replace this environment-file approach.
4. Review permissions for the configuration, CA bundle, working directory, every destination directory, state directory, and log directory.
5. Run credential-aware preflight through the system manager. This workflow assumes systemd 236 or newer for `--collect` and a manager whose `systemd-run --property` accepts `EnvironmentFile=` for transient service units; distribution backports vary, so verify both capabilities on the deployed system. PID 1 reads the root-owned `0600` file before applying `User=` and `Group=`, so the service account does not need direct permission to read it. The distinct transient unit names are collected automatically. The shell runs the active pass only if the dry-run service succeeds:

   ```bash
   sudo systemd-run --unit=nexus-jar-sync-preflight-dry-run --collect --wait --pipe \
     --property=Type=oneshot \
     --property=User=nexus-jar-sync \
     --property=Group=nexus-jar-sync \
     --property=WorkingDirectory=/opt/nexus-jar-sync \
     --property=EnvironmentFile=/etc/nexus-jar-sync/credentials.env \
     /opt/nexus-jar-sync/.venv/bin/python -m nexus_jar_sync.main \
     --config /etc/nexus-jar-sync/config.yaml --dry-run && \
   sudo systemd-run --unit=nexus-jar-sync-preflight-active --collect --wait --pipe \
     --property=Type=oneshot \
     --property=User=nexus-jar-sync \
     --property=Group=nexus-jar-sync \
     --property=WorkingDirectory=/opt/nexus-jar-sync \
     --property=EnvironmentFile=/etc/nexus-jar-sync/credentials.env \
     /opt/nexus-jar-sync/.venv/bin/python -m nexus_jar_sync.main \
     --config /etc/nexus-jar-sync/config.yaml
   ```

   This passes only the environment-file path—not its values—to systemd and does not use a shell expansion such as `env $(cat ...)`. Do not enable shell tracing around secret setup.

   If the installed systemd does not support `EnvironmentFile=` as a transient-unit property, use two temporary root-owned unit files under `/run/systemd/system/` based on the reviewed service template: give them distinct names, add `--dry-run` only to the first unit’s `ExecStart`, keep the same `User`, `Group`, `WorkingDirectory`, and `EnvironmentFile`, run the dry-run unit, run the active unit only after success, then remove both temporary units and run `systemctl daemon-reload`. Do not replace this with `sudo -u`, `source`, or `env $(cat ...)`, because those approaches either omit the service environment or risk exposing values.

These checks validate configuration, environment credentials, network/TLS/CA access, and destination, state, and log permissions before scheduling.

## Install and operate

Copy the templates, remove the `.example` suffix, and review the account, absolute paths, and interval:

```bash
sudo cp deployment/linux/nexus-jar-sync.service.example /etc/systemd/system/nexus-jar-sync.service
sudo cp deployment/linux/nexus-jar-sync.timer.example /etc/systemd/system/nexus-jar-sync.timer
sudo systemd-analyze verify /etc/systemd/system/nexus-jar-sync.service /etc/systemd/system/nexus-jar-sync.timer
sudo systemctl daemon-reload
sudo systemctl enable --now nexus-jar-sync.timer
```

`Type=oneshot` preserves the application exit status. Exit code `0` means success, `1` means one or more target failures, and `2` means a CLI/configuration/startup error. There is no `Restart=always`; a failed run remains visible and the next retry is the next timer activation. systemd does not start a second instance of the same service while it is active, so runs do not overlap. `Persistent=true` causes a missed timer activation to run after downtime. Change `OnBootSec` and `OnUnitActiveSec` to adjust the interval.

Inspect operations with:

```bash
systemctl list-timers nexus-jar-sync.timer
systemctl status nexus-jar-sync.service
journalctl -u nexus-jar-sync.service
```

Application file logs and the systemd journal may both contain operational information. Do not expose either in issue reports without review.

The template uses only broadly compatible hardening: `NoNewPrivileges=true` and `PrivateTmp=true`. Stronger directives such as `ProtectSystem`, `ProtectHome`, and `ReadWritePaths` require deployment-specific config, destination, state, log, and CA paths; add them only after verifying their read/write effects.

## Disable and remove

```bash
sudo systemctl disable --now nexus-jar-sync.timer
sudo rm /etc/systemd/system/nexus-jar-sync.timer /etc/systemd/system/nexus-jar-sync.service
sudo systemctl daemon-reload
```

Removing units does not remove application files, configuration, credentials, logs, state, or synchronized JARs. Configured destinations are bases containing exact-version child directories; deployed artifacts are append-only. Nexus access is GET-only, so grant the service account only repository browse/read permissions. Remove operational data separately only under your organization’s policy.

Install an organization-approved 7-Zip command separately when `extract_7z` companions are configured and set its absolute path in `tools.seven_zip_executable`. Before enabling the timer, run an isolated real test as the service identity with `--test-download --test-output /var/tmp/nexus-jar-sync-test-run-001`; it performs GET downloads and extraction but does not read or write production state or destinations.
