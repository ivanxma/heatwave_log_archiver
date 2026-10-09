# Archive control database

The login connection profile identifies the MySQL server used as the archive control database. Its named control schema holds the shared operational configuration and state. Any Compute running this app can connect to the same server and schema and retrieve the same jobs, connections, tables, history, schedule state, and extraction checkpoints.

## Storage

| Location | Contents |
|---|---|
| Local `profiles.json` | Control connection bootstrap: profile name, host/port or socket, management flag, control schema, worker username and credential Secret OCID; selected active worker profile |
| `<control_schema>.control_settings` | Job policy and archive setup values, in a singleton MySQL JSON row |
| `<control_schema>.control_entities` | Source/archive connection definitions, source/archive tables, mappings and custom sources, in ordered MySQL JSON records |
| `<control_schema>.control_state` | Execution history and per-source checkpoints (`job`), last successful scheduled run (`worker`), execution owner and cancellation request (`execution`) |
| Web process memory | Expiring login sessions, submitted login credentials, and cached database connections/Vault credentials |
| Configured archive database | Archived source records and their partitions |

The schema defaults to `archive_control`; operators may choose another valid MySQL schema identifier. Metadata uses JSON columns inside MySQL, rather than JSON files on Compute. Durable operational data has no automatic local-file fallback. If the control database is unavailable, operations fail instead of creating divergent settings on individual Computes.

The bootstrap connection profile remains local because the app needs it to locate the control schema. Plaintext passwords are not stored in that profile or in the control tables. Worker credentials are retrieved from the profile's `secret_ocid` using the instance principal. The credential may be shared with an archive connection if the account has the required permissions, but the control profile must reference it explicitly.

## Web setup

1. Sign in using the MySQL connection profile for the control server.
2. Open **Control DB**, or follow the first-login redirect there.
3. Enter the control schema, worker control user and credential **Secret OCID**.
4. Use **Test / validate Secret OCID** to check Vault retrieval, fresh MySQL authentication and runtime read/write permissions for an initialized schema. This does not save or activate the profile.
5. For an existing installation, select **Import this Compute's existing configuration, history, and checkpoints**. The target schema must be empty.
6. Confirm setup. The app creates the control tables, checks unattended credentials and stores the bootstrap selection in `profiles.json`.
7. Configure the archive destination. Initial archive setup requires archive credentials only; sources and jobs are defined afterward. A fresh installation keeps scheduled archival disabled until a job is enabled.

Opening ordinary authenticated pages uses the login session credentials for control database reads, without retrieving Vault credentials for each navigation. These pages now perform necessary control database queries. Archive report operations additionally use their configured archive credentials.

Creating a connection profile for an existing control schema also accepts the schema and worker Secret OCID. An administrative login activates that profile for the local scheduled worker. Use a separate profile when switching control schemas; changes must be coordinated with running workers. Sessions retain the profile they signed in with until logout.

## Command-line bootstrap and migration

For service deployments, run as the application service user and point to its profile store:

```bash
export ERROR_ARCHIVER_PROFILE_STORE=/var/lib/error-log-archiver/profiles.json
/opt/error-log-archiver/.venv/bin/python /opt/error-log-archiver/configure_control.py \
  --profile local3310 \
  --schema archive_control \
  --user '<control-worker-user>' \
  --secret-ocid '<control-credential-secret-ocid>' \
  --import-directory /var/lib/error-log-archiver
```

Stop the web service and timer and wait for any active archive service to finish before migrating. This prevents a legacy worker writing a newer checkpoint during import. Upgrade every Compute sharing the installation before enabling workers against the new control schema.

Import loads `settings.json`, `job-state.json`, and `worker-state.json`. Settings, entities, history and schedule state are inserted in one transaction, with the singleton settings row locked. Existing target configuration or job state is never overwritten. The importer verifies the database contents before activating the control profile. It then archives obsolete JSON stores and settings backups in a protected `legacy-control-backup.tar.gz` (mode `0600`) and removes the obsolete JSON files. `profiles.json` remains.

The rollback archive is historical data, not an active fallback. Keep it under restricted permissions until the migration is accepted. To roll back, first stop all new workers, restore the previous app and JSON files, and review any archival performed since migration before resuming.

## JSON exports

**Control DB** and **Job configuration** provide **Export worker control profile JSON** (`profiles.json`) and **Export job settings JSON** (`job-settings.json`). The worker profile includes the selected control schema and Secret OCID; no password is exported. It can be installed as the local bootstrap on another Compute. The job settings export contains the shared policy and source/archive definitions for reference or backup; runtime workers still load these definitions from the control database. Both downloads require an authenticated management profile.

## Adding another Compute

Install the same app version, create its local control connection profile, and use the same control server and schema. Configure the VM instance principal to read the control credential secret and the secrets used by jobs. Run the bootstrap command without `--import-directory`, or connect through **Control DB** without selecting import.

The second Compute does not need copies of settings, source/archive connections, history or checkpoints. Both Computes retrieve those from MySQL. Each web process has its own login sessions; sessions are not shared between hosts.

## Restoring job settings into a fresh control schema

1. Create/select the new control connection profile and initialize an empty control schema using **Control DB**. Test the worker credential Secret OCID first.
2. Open **Job configuration** before creating an archive destination or other settings in that schema.
3. Expand **Import job settings into a fresh control database**, select the exported `job-settings.json` (maximum 2 MiB), and confirm restoration including its saved scheduler enable state.
4. Click **Import job settings JSON**. The app checks JSON types, credential references, table identifiers, connection references and mappings without retrieving source/archive secrets. It obtains the shared execution lock, verifies the target is empty under a transactional row lock, and saves all policy and entity records in one transaction.
5. Check the configuration tabs and export again to verify the restored settings. An imported enabled policy applies on the next scheduled worker tick.

Files with plaintext passwords/private keys/tokens, worker `profiles.json` exports, invalid mappings, oversized files, and nonempty targets are rejected without replacing configuration. An active execution prevents import until it releases its lock. This action never changes the target control connection profile or imports execution history/checkpoints.

Archived data remains on the destinations referenced by the imported settings. This import does not recreate an entire database backup. A fresh control schema has no extraction checkpoints, so its first run reads from the retention floor and relies on existing archive fingerprints to suppress repeated records. The explicit Compute-JSON migration described above additionally restores history and checkpoints; the settings export/import is deliberately a configuration-only restore.

## Shared scheduling, locking and override

Every archive execution obtains a nonblocking MySQL `GET_LOCK` using a name derived from the control schema, on a dedicated connection. The lock is shared across web processes and Compute instances connected to the same MySQL server and schema. A busy lock is not bypassed.

The scheduled worker evaluates its due time after obtaining the lock. Archiving, successful checkpoint recording and scheduler state updates happen while it owns the lock. Settings read by an already running job apply to that run; later edits apply to the next run.

When a worker exits or its lock connection closes, MySQL releases the advisory lock automatically. No persistent file lock needs deletion after a process crash. During a network partition, release may depend on MySQL detecting the lost session.

**Job configuration → Override: stop current execution** requests cancellation of a specific execution ID. Workers check the shared cancellation state between batches and before committing ingestion and publishing checkpoints. A cancellation or changed ownership fails that run, releases its lock, and allows a subsequent run to start normally. The override never starts a second writer while the old lock is held. A blocked database operation must finish or fail before that worker reaches its next cancellation check. Already committed mappings and DDL are not undone; retry deduplication handles committed archive rows.

If a process has stopped, its old execution metadata may remain until the next lock owner replaces it, but that metadata does not itself hold a lock.

MySQL connection locks coordinate only clients on the same server. Separate servers with replicated schemas do not share `GET_LOCK`; do not run workers against separate control-server endpoints as if they had one shared lock.

## Permissions and updates

Initialization needs permission to create the selected schema and its tables. Runtime control access needs SELECT, INSERT, UPDATE and DELETE on that schema. The worker must also have the separate source-read and archive-maintenance permissions required by its jobs. MySQL must support the named-lock functions.

Settings saves are transactional; source/archive records are replaced atomically with their policy values. Runtime state updates lock their state row with `FOR UPDATE`, preventing concurrent history or cancellation updates from being lost. Simultaneous administrator configuration edits currently use last-save-wins behavior; there is no configuration revision conflict UI.

Back up the control schema as part of database administration. Deployments must keep the local control profile and service configuration available to both the web service and scheduled worker. The worker service explicitly sets `ERROR_ARCHIVER_PROFILE_STORE`.

## Verification

```bash
python -m unittest discover -s tests -v
```

The normal suite covers bootstrap profiles, navigation, archive setup, cancellation checks and lock cleanup using isolated tests. `tests/test_control_mysql.py` is opt-in (`ERROR_ARCHIVER_CONTROL_INTEGRATION=1`) and requires an explicitly supplied MySQL control target. It creates and removes a random disposable `archiver_test_*` schema and verifies settings roundtrips, separate-process concurrent updates, cross-process lock exclusion, crash cleanup, cancellation and import protection. It never runs against the production control schema.
