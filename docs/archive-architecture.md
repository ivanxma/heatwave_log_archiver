# Archive architecture and lifecycle workflow

## Components

```text
MySQL source hosts                    OCI Vault
error_log / slow_log / general_log ── credential secret
custom tables or views                     │
        │                                  │ instance principal
        └──── scheduled worker ────────────┘
                    │
                    ▼
        partitioned archive MySQL table
                    │
                    ▼
        HTTPS web console / lifecycle actions
```

The Linux 9 timer evaluates the schedule every minute. When due, the worker loads non-secret job settings, obtains source/archive credentials from OCI Vault, and processes each configured source independently.

## OCI Vault secure-ID flow

1. Store source and archive MySQL credentials in OCI Vault. A secret may be JSON (`username` and `password`) or password text when username is configured separately.
2. Put the VM in a dynamic group and permit it to read the chosen secrets, for example: `Allow dynamic-group <group> to read secret-bundles in compartment <compartment>`.
3. Enter only Secret OCIDs in **Job configuration** or **Archive DB setup**. No password is written to configuration, process logs, or browser data.
4. Each run uses OCI instance-principal signing to retrieve and decode the secret in process memory, then discards it when the worker exits.

## Idempotent archive execution

Built-in sources are `performance_schema.error_log`, `mysql.slow_log`, and `mysql.general_log`; optional custom tables/views supply their own timestamp column. Every source keeps an independent timestamp cursor.

For each row, the worker serializes its payload and derives a SHA-256 fingerprint from source identity, event timestamp, and payload. The archive key is `(event_time, source_fingerprint)`, and writes use `INSERT IGNORE`. This makes timer retries, manual runs, and recovered batches idempotent: previously archived rows are ignored rather than duplicated.

## Lifecycle management workflow

1. Use **Prepare future partitions** to create the selected number of upcoming monthly partitions.
2. Each cycle drops fully expired month partitions according to retention using `ALTER TABLE ... DROP PARTITION`.
3. Select partitions in the web console to export as a single ZIP of CSV files, empty them, or delete them after approval.
4. Filter archived records by source and partition to validate the lifecycle before deletion.

Dropping a partition is irreversible; emptying one retains its boundary for newly arriving records.
