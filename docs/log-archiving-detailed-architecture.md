# Detailed log-archiving architecture

## Purpose and scope

HeatWave Log Archiver is a Linux 9 service and HTTPS console that copies selected MySQL log sources into a separately configurable archive MySQL database. It is intended for demonstration and tutorial use; production operators must validate availability, capacity, IAM, encryption, monitoring, backup, and retention requirements for their environment.

The service supports these built-in sources:

| Archive source | MySQL object | Incremental timestamp |
| --- | --- | --- |
| Error log | `performance_schema.error_log` | `LOGGED` |
| Slow log | `mysql.slow_log` | `start_time` |
| General log | `mysql.general_log` | `event_time` |

An operator can also add multiple custom tables or views with a configured timestamp column. Standard source labels are stored as `error_log`, `slow_log`, and `general_log`; custom sources use their configured source/mapping name. A single job can archive several sources during one scheduled execution.

## Deployment topology

```text
Browser
  │ HTTPS :443
  ▼
Error Log Archiver web service ──────► /var/lib/error-log-archiver
  │                                            non-secret settings
  │ manages schedule, sources, lifecycle        job state/history
  │
  ├── OCI instance principal ───────► OCI Vault secret bundles
  │                                      source/archive credentials
  │
systemd timer ─► archive worker ─────► Source MySQL host(s)
 every minute        when due              selected log tables/views
                         │
                         └────────────────► Archive MySQL host
                                               partitioned archive table
```

The web service runs on HTTPS port 443 as a non-root service user. The systemd unit grants only `CAP_NET_BIND_SERVICE` for the privileged listener port. The archive worker is a separate, one-shot systemd service invoked by a persistent timer.

## Scheduling and execution

The systemd timer wakes every minute. It does not dictate the business interval itself; instead, the worker reads the web-configured interval, for example `5min` or `1hour`, and evaluates whether the next run is due.

This design has two operational benefits:

1. Changing the interval in the console applies on the next timer evaluation without rewriting/reloading a systemd timer unit.
2. `Persistent=true` allows systemd to evaluate missed timer activity after a host restart.

The worker records execution status, timestamp, source cursor information, inserted-record count, and partition changes in durable job state. The dashboard presents the latest summary, execution history, and 24-hour archive activity chart.

The timer and the web-triggered **Run archive now** operation share a non-blocking host lock. When another execution owns the lock, the attempted run is recorded as skipped rather than overlapping the active archive cycle.

## Credential and security design

### Secret boundaries

Database passwords are not persisted in application configuration, source control, browser storage, logs, or systemd environment files. The only credentials accepted at runtime for scheduled jobs come from OCI Vault.

The configuration stores only:

- reusable source/archive hosts, ports, sockets, usernames, archive database/tables, and mappings;
- source table type/definition, schedule, retention, batch size, worker count, and mapping enablement;
- OCI Vault Secret OCIDs.

At execution time the worker initializes `InstancePrincipalsSecurityTokenSigner`, reads the requested Secret Bundle, base64-decodes the content in process memory, opens MySQL connections, and drops the process memory when the worker exits.

Vault secret content can be a password string or JSON:

```json
{"username":"archive_user","password":"secret-value"}
```

The OCI policy must allow the VM’s dynamic group to read the selected secret bundles. A least-privilege policy should scope access to the applicable compartment and secrets.

### Interactive login

Connection profiles contain non-secret connection defaults. Browser cookies contain only an opaque session identifier. The MySQL password entered during interactive sign-in remains in an in-memory, expiring server-side session; it is not rendered back into forms or saved to a file.

All state-changing web forms carry a CSRF token. The application returns a clear refresh message when a standalone Login or Create Profile form token has expired.

## Archive data model

The archive table uses a normalized envelope so several source types can share a single lifecycle:

| Column | Purpose |
| --- | --- |
| `event_time` | Source record timestamp and partition key |
| `log_type` | `error_log`, `slow_log`, `general_log`, or configured custom source identity |
| `payload` | JSON representation of original source row columns |
| `archived_at` | Archive insertion time |
| `source_fingerprint` | SHA-256-derived row identity |

The primary key is `(event_time, source_fingerprint)`. The table is partitioned by monthly `RANGE COLUMNS(event_time)` partitions.

## Incremental ingestion and duplicate prevention

Each configured source maintains its own timestamp cursor in durable job state. On a run, the worker reads source rows in ascending timestamp order, starting at the retention floor for a new source or after that source’s stored cursor for a resumed source. It loops across batches, so batch size bounds source reads rather than imposing a total execution limit.

For every source row, the service creates a canonical JSON payload and derives a SHA-256 input from:

```text
source identity | event timestamp | canonical source payload
```

The worker uses `INSERT IGNORE` into the archive table. If a record is seen again because of a retry, manual run, timer overlap, or interrupted execution, the existing primary key causes MySQL to ignore it. This gives the process idempotent, at-least-once source reads with effectively-once archive storage semantics for the defined fingerprint.

The timestamp cursor is recorded after source rows have been processed. Operators should ensure custom timestamp columns are suitable for monotonically progressing extraction. If a source produces more than one batch with identical timestamps, a source-specific immutable key should be included in a future cursor enhancement to guarantee no timestamp-tie gaps.

## Partition lifecycle management

Partition management is deliberately exposed as a business workflow:

1. At setup, the service creates the archive database/table after explicit confirmation.
2. The worker ensures partitions for the retention window and a configurable future runway.
3. **Prepare future partitions** adds a user-selected number of empty upcoming monthly partitions.
4. Retention removes fully expired monthly partitions with `ALTER TABLE ... DROP PARTITION`, avoiding row-by-row deletes.
5. The console can filter a partition, export selected partitions as a ZIP containing CSV files, empty selected partitions, or permanently delete selected partitions.

Emptying a partition retains its boundary for new records. Dropping it removes both the rows and partition definition; it is irreversible and should follow export/approval policy.

## Web console workflows

| Page | Responsibilities |
| --- | --- |
| Archive | Status summary, 24-hour activity, immediate execution, paged/filterable archive entries, partition lifecycle operations |
| Job configuration | Job Policy plus Source Connection, Archive Connection, Source Tables, Archive Tables, and Mapping tabs; references are validated and names are unique per record type |
| Archive DB setup | Configure a local or remote archive database and confirm schema/partition creation |
| Connection profiles | Create/select non-secret MySQL connection defaults for interactive administration |

Archive entries can be narrowed by archive table, archive source, and monthly partition. The report table supports filter, page sizing, CSV download, layout reset, column sorting, resizing, reordering, and full payload viewing. Archive lookup failures are displayed in the applicable Entries or Partitions tab.

## Failure handling and operations

The worker logs exceptions to the systemd journal and records a failed execution state without logging credential values. Operators should monitor:

- `systemctl status error-log-archiver.timer error-log-archiver.service`;
- `journalctl -u error-log-archiver.service`;
- dashboard execution status and 24-hour chart;
- Vault access policies and Secret OCID correctness;
- archive table storage growth and retention success.

Recommended lifecycle runbook:

1. Validate a new source with a short manual run.
2. Confirm row counts, source selection, and partition placement in the dashboard.
3. Prepare future partitions before expected high-volume periods.
4. Export partitions required for long-term retention.
5. Empty or drop partitions only after the retention/export approval process.
6. Rotate Vault secrets through OCI Vault; the next worker execution retrieves the current secret value without application configuration changes.
