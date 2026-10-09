# MySQL Log Archiver

> **Demo and tutorial purpose:** This project is designed to demonstrate and teach secure MySQL log archival, OCI Vault integration, partition lifecycle management, and operational reporting. Review, test, and adapt its policies, privileges, retention, capacity, and TLS configuration before using it for production workloads.

Linux 9 service and HTTPS web console for archiving MySQL error, slow, general, and optional custom table/view sources into a partitioned archive database.

## Security model

Scheduled-job database passwords are never persisted in application files, source code, browser storage, or logs. At execution time the service retrieves source and archive credentials from OCI Vault using the Compute instance principal. Credentials may exist briefly in protected process memory while a connection is active; configuration stores only non-secret connection details and Vault Secret OCIDs.

Opening the summary, configuration, or setup pages reads settings from the archive control schema using session credentials, without contacting OCI Vault. Login and database operations still connect as needed; form actions periodically check login-profile connectivity. Archive entries and partition reports retrieve only archive credentials, cached in server memory for 300 seconds by default (`ERROR_ARCHIVER_VAULT_CACHE_SECONDS`). Credential changes clear that cache. Secret OCIDs must start with `ocid1.vaultsecret.`; vault OCIDs are rejected before calling OCI.

The web service runs one Gunicorn process with eight request threads. Threads keep idle HTTPS connections and slow database requests from blocking ordinary menu navigation. Keep one worker process because login sessions are stored in process memory.

Successful archive setup registers the destination under **Job configuration → Archive Connection** and **Archive Tables**. Repeating setup reuses matching records. **Archive DB setup** shows the saved connection and database/table in its editable form without fetching Vault credentials; confirming setup connects to create or verify the archive schema.

Source extraction replays the saved timestamp with `>=` and streams one query in batches, avoiding gaps between rows with identical timestamps. See [Incremental source retrieval and timestamp boundaries](docs/incremental-source-retrieval.md) for the algorithm, duplicate handling, diagnostics, and limits.

Create each Vault secret as either a plain password or JSON:

```json
{"username":"archive_user","password":"replace-me"}
```

Grant the VM dynamic group permission to read the control, source and archive Secret OCIDs, then use **Job configuration** to enter source/archive Vault Secret OCIDs and enable the job. See the [detailed log-archiving architecture](docs/log-archiving-detailed-architecture.md) for deployment, security, idempotency, and lifecycle details.

### Required OCI IAM policy

The Compute VM must be a member of an OCI **dynamic group** because the scheduled worker uses an instance principal. Create a dynamic-group matching rule that scopes membership to this VM or, where appropriate, its dedicated Compute compartment. For example:

```text
ALL {instance.id = '<compute-instance-ocid>'}
```

Attach a least-privilege policy in the compartment that contains the Vault secrets. Prefer individual Secret OCIDs when source and archive credentials are known:

```text
Allow dynamic-group <archiver-dynamic-group> to read secret-bundles in compartment <vault-compartment> where target.secret.id = '<control-secret-ocid>'
Allow dynamic-group <archiver-dynamic-group> to read secret-bundles in compartment <vault-compartment> where target.secret.id = '<source-secret-ocid>'
Allow dynamic-group <archiver-dynamic-group> to read secret-bundles in compartment <vault-compartment> where target.secret.id = '<archive-secret-ocid>'
```

If operationally necessary, the broader alternative is `Allow dynamic-group <archiver-dynamic-group> to read secret-bundles in compartment <vault-compartment>`. Do not grant `manage secret-family`, tenancy-wide secret access, or unrelated resource permissions to the archiver. IAM policy propagation can take a short time; validate access from the VM with an instance-principal Secret Bundle read before enabling the scheduled job.

## Install on a new Oracle Linux 9 VM

Before connecting, allow TCP 22 from your administration IP in the OCI NSG/security list. Clone and install:

```bash
sudo dnf install -y git
sudo git clone https://github.com/ivanxma/heatwave_log_archiver /opt/error-log-archiver
cd /opt/error-log-archiver
sudo ./setup.sh
```

For updates, setup stops the timer and web service before replacing dependencies and restarts them afterward. If an archive cycle is active, it stops the timer and asks you to rerun after that cycle finishes. Existing JSON data remains until explicitly imported through **Control DB** or the migration command. Keep the checkout root-owned and run `cd /opt/error-log-archiver && sudo git pull --ff-only && sudo ./setup.sh`.

The console listens only on HTTPS port 443. Setup opens the host firewalld HTTPS service when available, but OCI ingress is separate: allow TCP 443 in the VM's NSG/security list. Setup generates a self-signed certificate under `/etc/error-log-archiver/tls/`; replace it with a trusted certificate for production use.

After installation, browse to `https://<public-ip>/`, create/select a control connection profile, configure its control schema and worker Secret OCID, configure the archive destination, then define and enable jobs.

## Archive control database

The connection profile identifies the MySQL archive control server. Job settings, source/archive connections, table mappings, execution history and checkpoints live in its named control schema; only the bootstrap connection profile remains on Compute. A second Compute connects to the same schema to retrieve everything. See [Archive control database](docs/archive-control-database.md) for setup, migration, shared locking and cancellation override.

### Initial setup, validation and JSON export

**Control DB** is the first setup screen. Login checks that the configured control schema has its required tables and settings row. A profile naming a new or uninitialized schema redirects to setup before activating the scheduled worker; a schema name alone does not initialize it. Enter the control schema and the credential **Secret OCID** (`ocid1.vaultsecret.…`). **Test / validate Secret OCID** retrieves the secret, establishes a fresh MySQL connection and, for an initialized schema, tests read/write access using rolled-back probe operations. Testing does not save or activate the profile or create a schema. **Create or connect control schema** initializes missing tables, validates access and activates the worker bootstrap profile.

Use **Export worker control profile JSON** to download a password-free `profiles.json` containing the active control profile, schema and credential Secret OCID. Install it on another Compute as `/var/lib/error-log-archiver/profiles.json`, owned by `errorlogarchiver` with mode `0600`. That worker retrieves all operational settings from the shared schema. **Export job settings JSON** downloads the policy and source/archive definitions as a portable configuration snapshot, without plaintext credentials; it is not a replacement for the worker bootstrap profile.

To restore that snapshot into a fresh control database, configure and validate its control connection first, then open **Job configuration → Import job settings into a fresh control database**. Select `job-settings.json`, confirm the saved scheduler enable state, and import. Validation runs before the transaction writes settings. Existing configuration is never overwritten. History, extraction checkpoints, archive data, and the local control profile are not restored by this settings import; see [restoring settings](docs/archive-control-database.md#restoring-job-settings-into-a-fresh-control-schema).

Setup can bootstrap an already-defined local profile non-interactively:

```bash
sudo env ERROR_ARCHIVER_CONTROL_PROFILE=local3310 \
  ERROR_ARCHIVER_CONTROL_SCHEMA=archive_control \
  ERROR_ARCHIVER_CONTROL_USER='<control-worker-user>' \
  ERROR_ARCHIVER_CONTROL_SECRET_OCID='<control-credential-secret-ocid>' \
  ERROR_ARCHIVER_IMPORT_LEGACY=1 ./setup.sh
```

Set `ERROR_ARCHIVER_IMPORT_LEGACY=1` only for the first migration into an empty schema. Omit it on later updates and on a second Compute using that shared schema. The profile must already exist locally. Without these variables, use the control setup UI; workers without an active control profile remain disabled.

## Technical operation guide

See [MySQL Log Archiver: technical operations](docs/log-archiver-operations.md) for the worker flow, archive envelope, monthly partition creation and retention, incremental selection/checkpoints, control database contents, JSON export/import recovery, and implemented protections with their limits.

Partitions use `RANGE COLUMNS(event_time)` with names such as `p202610` and an exclusive next-month boundary. Every archive run prepares the retention window plus two future months before ingestion, then prunes expired monthly partitions after ingestion. With 12-month retention in October 2026, the cutoff is October 1, 2025. MySQL does not create these partitions by itself; an enabled archive run or explicit preparation action performs maintenance.

Save settings before exporting: the export downloads the saved control database configuration and does not write unsaved values. Import into a fresh control schema restores configuration only, including its enabled state; it does not restore history, checkpoints or archive rows. Use the worker-profile export when adding a Compute to an existing shared control schema.

## Operations

- **Archive DB setup** configures a local or remote archive MySQL destination and creates the schema/table/partitions after confirmation.
- **Job configuration** has Source Connection, Archive Connection, Source Tables, Archive Tables, and Mapping tabs. A source table maps to one archive table; mappings can be enabled or disabled. The Job Policy block controls enablement, interval, retention, batch size, and worker concurrency.
- **Source types** provide Error Log, General Log, Slow Log, and Custom presets. Standard records are labelled `error_log`, `general_log`, or `slow_log`; custom records use their configured name without a `custom:` prefix.
- **Archive** uses a three-tab view: **Summary** shows current scheduler status, metric blocks, execution history, and a labelled 24-hour chart; **Archived entries** provides paged, archive-table/source/partition-filtered records with export and reset-layout controls; **Partitions** provides lifecycle actions and partition downloads for the selected archive table.
- POST forms use CSRF tokens. A shared MySQL lock prevents concurrent execution across Computes. The configuration screen provides a cancellation override.
- The systemd timer wakes every minute; the worker applies the configured interval without a service restart.

View worker activity with `journalctl -u error-log-archiver.service`.

### Log Explore

Open **Log Explore**, choose a saved archive connection and one of its configured tables. No archive connection or Vault lookup is made until a table is selected. JSON objects and arrays expand into columns such as `payload.message` and `payload.tags[0]`; fields available on the current page determine its columns. Optional filter rules choose fields, operators and values, with All (AND) or Any (OR) matching; column headings sort the whole selected archive in MySQL. Page sizes are 25, 50, 100 or 250. Previous/Next and the page-number control browse results without a full row count. Drag column headings and resize their edges; layouts persist in your browser per connection/table. The reset icon restores the layout. The download icon exports the displayed page as CSV with expanded fields.

Choose **Bar chart** to count records using `event_time`, grouped by hour, day, Monday-based week or month in UTC. Select an inclusive date range (up to 367 days) and apply optional filter rules if needed. Empty intervals appear as zero; charts are limited to 1,000 buckets. Charts fit the available panel width for every interval and redraw when the window resizes. Move across the chart for exact counts, expand the counts table or download the aggregation as CSV. Boundary weeks/months count only records within the selected dates.

Reads use bounded pages and charts use a timestamp range. Field filters and JSON field sorts can still scan matching rows on large archives; use a shorter chart range when needed. Browsing is read-only and does not change job settings or checkpoints.

### Source identity and optional rules

The archiver reads `@@server_uuid` and `@@hostname` from the connected source instance. Newly inserted archive rows store `source_server_uuid`, `source_hostname`, logical `source_connection` and `source_table`. A source connection records its latest instance and all observed UUIDs in the control database; Source Connection configuration displays the UUID and hostname. Saving a source connection validates its Vault credential/MySQL connection and captures its identity; ingestion refreshes identity observations automatically.

Failover or switchover can change the UUID/hostname behind the same IP. This does not stop ingestion or reset that logical source's checkpoint. Existing fingerprints remain unchanged, so replays across cluster instances still deduplicate. Old archive tables gain nullable identity columns and a UUID/time index when prepared; historical rows without captured identity remain untracked rather than being attributed to a guessed server.

In **Log Explore**, only the archive connection and table select the destination. Source connection is an **optional filter rule**, with multi-select values. No source rule means all sources; choosing a logical connection includes every UUID observed for it. Choose **Legacy / untracked source** to find rows without UUID metadata. Click a record's UUID or source connection to see its recorded source table/hostname and matching registered connection details. Rules are preserved through sorting, paging, chart views and CSV downloads. The fingerprint footnote explains the stored hash: SHA-256 of `cursor_key|event_time|canonical_payload`, using `json.dumps(row, default=str, sort_keys=True)` for the payload, displayed as 64 hexadecimal characters; Log Explore does not recompute it.
