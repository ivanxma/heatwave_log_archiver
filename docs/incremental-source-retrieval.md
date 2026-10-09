# Incremental source retrieval and timestamp boundaries

## Purpose

Each source has a saved timestamp checkpoint. Extraction must include records that share that timestamp without skipping rows at a batch boundary, repeatedly querying the same rows, or inserting duplicates into the archive.

The implementation is in `modules/archive_service.py`, function `archive_error_log`. It applies to the error, general, and slow logs and configured custom source tables/views.

## Query and batch handling

For a resumed error-log source, the worker issues one query:

```sql
SELECT *
FROM performance_schema.error_log
WHERE LOGGED >= %s
ORDER BY LOGGED ASC;
```

The parameter is that source's saved checkpoint. General logs use `event_time`, slow logs use `start_time`, and custom sources use their configured timestamp column. On the first run, the lower bound is the first day of the month at the configured retention cutoff instead.

The Connector/Python cursor is explicitly unbuffered (`dictionary=True, buffered=False`). The worker consumes the result using `fetchmany(batch_size)` until it returns no rows. There is no SQL `LIMIT`, repeated timestamp query, or offset pagination. `batch_size` bounds each group processed in Python; it does not cap the number of rows archived in a run or the work MySQL performs to execute the query. The run drains the entire qualifying result.

Example with batch size 2:

```text
Saved checkpoint: 08:00:00
Source rows: A, B, C, D, E all have timestamp 08:00:00
Fetched groups: [A, B], [C, D], [E], []
```

All five records are processed even though their timestamps are identical. Advancing within one result stream avoids needing a unique row ID for these log sources. No ordering between equal-timestamp rows is required: the stream consumes all rows in the result.

## Replaying the boundary and deduplication

Every run includes the saved timestamp with `>=`. A record that becomes visible later with exactly the checkpoint timestamp is eligible on the next run, provided that checkpoint has not already advanced beyond its timestamp.

The archive insertion remains:

```sql
INSERT IGNORE INTO archive_table
    (event_time, log_type, payload, source_fingerprint)
VALUES (%s, %s, %s, UNHEX(SHA2(%s, 256)));
```

The fingerprint input is unchanged:

```text
source cursor identity | event timestamp | canonical JSON payload
```

The archive primary key `(event_time, source_fingerprint)` suppresses repeat inserts. Existing checkpoint keys and fingerprints remain compatible; no schema change or checkpoint reset is needed.

A batch inserting zero records usually contains only previously archived rows. It is not evidence that the batch size is insufficient. The worker logs an INFO diagnostic with the source identity, rows read, rows ignored, and configured batch size, then continues reading. `INSERT IGNORE` can also ignore rows for database conditions other than duplicate keys, so the diagnostic describes ignored records without treating every ignored row as a proven duplicate. Actual exceptions remain errors and fail the cycle.

Increasing batch size can reduce the number of fetch calls; it is not necessary to get past timestamp ties or duplicate-only batches. A full duplicate batch followed by new rows must still archive the new rows.

## Checkpoint and retry behavior

The worker tracks the last processed timestamp, including ignored rows. After all selected sources have been read, the archive transaction commits and the function returns its checkpoints. The scheduled worker persists these checkpoints in the shared control schema (`control_state`, job record) with the successful cycle result.

A failed write does not commit that extraction transaction or publish a new checkpoint. If some archive transactions have already committed but the overall run fails before checkpoint persistence, a retry replays them and relies on fingerprint deduplication. This also covers a process failure between the archive commit and saving job state.

## Limits

- This fixes rows skipped by timestamp-only pagination at equal-timestamp batch boundaries. It does not recover previously missed rows older than the current checkpoint automatically.
- Rows arriving with timestamps older than the current checkpoint remain outside the query. Capturing those requires a defined overlap window, an insertion sequence, or change data capture.
- Source records must remain available until extraction reads them. Log truncation, ring-buffer eviction, and concurrent changes to nontransactional sources can prevent complete capture. Streaming does not make every source a stable snapshot.
- Two source records with identical timestamp and canonical payload share the same fingerprint and are stored once. This behavior predates this change.
- `SELECT *` requests every column of qualifying rows; it does not mean the query requests every source row. However, a timestamp predicate does not guarantee an index range scan. Actual scan and sort costs depend on the source and its query plan.
- Unbuffered fetching bounds client-side result buffering. It does not bound server-side sorting or transaction size; all qualifying rows are processed in the same extraction transaction.

## Verification

Run:

```bash
python -m unittest discover -s tests -v
```

`tests/test_incremental_archive.py` covers ties across several batches, new boundary rows, replay deduplication, a full duplicate batch followed by new rows, retries with an old checkpoint, empty results, filtering older records, and failed writes without commit. These are isolated extraction tests, not a validation of every MySQL source's execution plan or concurrency behavior.
