# HeatWave Log Archiver

> **Demo and tutorial purpose:** This project is designed to demonstrate and teach secure MySQL log archival, OCI Vault integration, partition lifecycle management, and operational reporting. Review, test, and adapt its policies, privileges, retention, capacity, and TLS configuration before using it for production workloads.

Linux 9 service and HTTPS web console for archiving MySQL error, slow, general, and optional custom table/view sources into a partitioned archive database.

## Security model

The service never stores scheduled-job database passwords in application state, source code, runtime environment, browser storage, or logs. At execution time it retrieves source and archive credentials from OCI Vault using the Compute instance principal. Configuration stores only non-secret connection details and Vault Secret OCIDs.

Create each Vault secret as either a plain password or JSON:

```json
{"username":"archive_user","password":"replace-me"}
```

Grant the VM dynamic group permission to read the Secret OCIDs, then use **Job configuration** to enter source/archive Vault Secret OCIDs and enable the job. See the [detailed log-archiving architecture](docs/log-archiving-detailed-architecture.md) for deployment, security, idempotency, and lifecycle details.

## Install on a new Oracle Linux 9 VM

Before connecting, allow TCP 22 from your administration IP in the OCI NSG/security list. Clone and install:

```bash
sudo dnf install -y git
sudo git clone https://github.com/ivanxma/heatwave_log_archiver /opt/error-log-archiver
cd /opt/error-log-archiver
sudo ./setup.sh
```

For updates, keep the checkout root-owned and run `cd /opt/error-log-archiver && sudo git pull --ff-only && sudo ./setup.sh`.

The console listens only on HTTPS port 443. Setup opens the host firewalld HTTPS service when available, but OCI ingress is separate: allow TCP 443 in the VM's NSG/security list. Setup generates a self-signed certificate under `/etc/error-log-archiver/tls/`; replace it with a trusted certificate for production use.

After installation, browse to `https://<public-ip>/`, create/select a non-secret connection profile, set OCI Vault Secret OCIDs, configure the archive destination, then enable the job.

## Operations

- **Archive DB setup** configures a local or remote archive MySQL destination and creates the schema/table/partitions after confirmation.
- **Job configuration** enables/disables the job, selects multiple built-in logs, maintains optional table/view sources, controls schedule, retention, and batch size.
- **Archive** uses a three-tab view: **Summary** shows current scheduler status and a 24-hour records-archived chart; **Archived entries** provides paged, source- and partition-filtered records with export and reset-layout controls; **Partitions** provides lifecycle actions and partition downloads.
- The systemd timer wakes every minute; the worker applies the configured interval without a service restart.

View worker activity with `journalctl -u error-log-archiver.service`.
