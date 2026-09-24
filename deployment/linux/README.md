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
5. Run both preflight commands as the intended service account:

   ```bash
   sudo -u nexus-jar-sync /opt/nexus-jar-sync/.venv/bin/python -m nexus_jar_sync.main --config /etc/nexus-jar-sync/config.yaml --dry-run
   sudo -u nexus-jar-sync /opt/nexus-jar-sync/.venv/bin/python -m nexus_jar_sync.main --config /etc/nexus-jar-sync/config.yaml
   ```

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

Removing units does not remove application files, configuration, credentials, logs, state, or synchronized JARs. Remove those separately only under your organization’s data-retention policy.
