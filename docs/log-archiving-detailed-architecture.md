# Detailed log-archiving architecture

## Purpose and scope

MySQL Log Archiver is a Linux 9 service and HTTPS console that copies selected MySQL log sources into a separately configurable archive MySQL database. It is intended for demonstration and tutorial use; production operators must validate availability, capacity, IAM, encryption, monitoring, backup, and retention requirements for their environment.

The service supports these built-in sources:

| Archive source | MySQL object | Incremental timestamp |
| --- | --- | --- |
| Error log | `performance_schema.error_log` | `LOGGED` |
| Slow log | `mysql.slow_log` | `start_time` |
| General log | `mysql.general_log` | `event_time` |

An operator can also add multiple custom tables or views with a configured timestamp column. Standard source labels are stored as `error_log`, `slow_log`, and `general_log`; custom sources use their configured source/mapping name. A single job can archive several sources during one scheduled execution.

## Deployment topology

```text
Browser ── HTTPS :443 ──► MySQL Log Archiver web service
                              │
                              ├──► Shared MySQL control schema (settings/state)
                              └──► Archive MySQL (reports/exploration)

Local profiles.json ──► Archive worker ◄── systemd timer
                              │
                              ├──► OCI Vault (control/source/archive credentials)
                              ├──► Shared MySQL control schema (schedule/checkpoints/lock)
                              ├──► Source MySQL table(s)
                              └──► Archive MySQL (records/monthly partitions)
```

The web service runs on HTTPS port 443 as a non-root service user. The systemd unit grants only `CAP_NET_BIND_SERVICE` for the privileged listener port. The archive worker is a separate, one-shot systemd service invoked by a persistent timer.

## Scheduling and execution

The systemd timer wakes every minute. It does not dictate the business interval itself; instead, the worker reads the web-configured interval, for example `5min` or `1hour`, and evaluates whether the next run is due.

This design has two operational benefits:

1. Changing the interval in the console applies on the next timer evaluation without rewriting/reloading a systemd timer unit.
2. The boot trigger resumes evaluation after a restart; the saved due time lives in the control database. Missed intervals are not replayed as individual executions.

The worker records execution status, timestamp, source cursor information, inserted-record count, and partition changes in durable job state. The dashboard presents the latest summary, execution history, and 24-hour archive activity chart.

The timer and web-triggered **Run archive now** operation share a nonblocking MySQL connection lock across Computes using the same control server and schema. Busy runs do not overlap. An administrator can request cancellation between batches. See [Archive control database](archive-control-database.md) for shared storage, bootstrap and migration.

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
| `source_server_uuid`, `source_hostname` | Actual source instance observed during retrieval |
| `source_connection`, `source_table` | Logical source connection and source table at insertion |

The primary key is `(event_time, source_fingerprint)`. The table is partitioned by monthly `RANGE COLUMNS(event_time)` partitions.

## Incremental ingestion and duplicate prevention

Each configured source maintains its own timestamp cursor in durable job state. On a run, the worker reads source rows in ascending timestamp order, starting at the retention floor for a new source or including that source’s stored cursor with `>=` for a resumed source. It consumes one unbuffered query result with `fetchmany(batch_size)` until exhausted, including all timestamp ties across batches. Batch size bounds client-side fetch groups rather than imposing a total execution limit.

For every source row, the service creates a canonical JSON payload and derives a SHA-256 input from:

```text
source identity | event timestamp | canonical source payload
```

The worker uses `INSERT IGNORE` into the archive table. If a record is seen again because of a retry, manual run, timer overlap, or interrupted execution, the existing primary key causes MySQL to ignore it. Replays are idempotent for that fingerprint. Source availability and timestamp ordering still determine which records are eligible; this is not a guarantee of complete source capture.

The timestamp cursor is persisted after archive commit and successful cycle completion. A duplicate-only batch does not stop extraction. Records arriving with timestamps older than the saved cursor still require an overlap or another extraction strategy. See [Incremental source retrieval and timestamp boundaries](incremental-source-retrieval.md) for the query, duplicate diagnostics, retry behavior, and limitations.

## Partition lifecycle management

Partition management is deliberately exposed as a business workflow:

1. At setup, the service creates the archive database/table after explicit confirmation.
2. The worker ensures partitions for the retention window and two future months by default.
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
| Control DB | Initialize/validate the shared control schema and worker bootstrap; export the control profile |
| Log Explore | Expand archived JSON into sortable columns, search/page/export records and chart counts by time |
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

For the current operational runbook, partition boundaries, configuration export/import and protection limits, see [technical operations](log-archiver-operations.md).
