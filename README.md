# HeatWave Log Archiver

> **Demo and tutorial purpose:** This project is designed to demonstrate and teach secure MySQL log archival, OCI Vault integration, partition lifecycle management, and operational reporting. Review, test, and adapt its policies, privileges, retention, capacity, and TLS configuration before using it for production workloads.

Linux 9 service and HTTPS web console for archiving MySQL error, slow, general, and optional custom table/view sources into a partitioned archive database.

## Security model

The service never stores scheduled-job database passwords in application state, source code, runtime environment, browser storage, or logs. At execution time it retrieves source and archive credentials from OCI Vault using the Compute instance principal. Configuration stores only non-secret connection details and Vault Secret OCIDs.

Create each Vault secret as either a plain password or JSON:

```json
{"username":"archive_user","password":"replace-me"}
```

Grant the VM dynamic group permission to read the Secret OCIDs, then use **Job configuration** to enter source/archive Vault Secret OCIDs and enable the job. See [archive architecture](docs/archive-architecture.md) for the concise workflow and the [detailed log-archiving architecture](docs/log-archiving-detailed-architecture.md) for deployment, security, idempotency, and lifecycle details.

## Install on Oracle Linux 9

Copy the repository to `/opt/error-log-archiver` and run `sudo ./setup.sh`.

The console listens only on HTTPS port 443. Setup generates a self-signed certificate under `/etc/error-log-archiver/tls/`; replace it with a trusted certificate for production use.

## Operations

- **Archive DB setup** configures a local or remote archive MySQL destination and creates the schema/table/partitions after confirmation.
- **Job configuration** enables/disables the job, selects multiple built-in logs, maintains optional table/view sources, controls schedule, retention, and batch size.
- **Archive** shows execution summary, 24-hour activity, paged source/partition-filtered records, and partition lifecycle controls.
- The systemd timer wakes every minute; the worker applies the configured interval without a service restart.

View worker activity with `journalctl -u error-log-archiver.service`.
