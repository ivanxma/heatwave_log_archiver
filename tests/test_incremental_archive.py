"""Exercise extraction with timestamp ties, retries, and late boundary rows."""
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from modules.archive_service import archive_error_log


class SourceCursor:
    def __init__(self, rows):
        self.rows = rows
        self.queries = []
        self.fetch_sizes = []

    def execute(self, sql, parameters):
        self.queries.append((sql, parameters))
        assert ' >= %s ' in sql
        assert 'LIMIT' not in sql
        floor = parameters[0]
        if isinstance(floor, str):
            floor = datetime.fromisoformat(floor)
        elif not isinstance(floor, datetime):
            floor = datetime.combine(floor, datetime.min.time())
        self.result = sorted((dict(row) for row in self.rows if row['LOGGED'] >= floor), key=lambda row: row['LOGGED'])

    def fetchmany(self, size):
        self.fetch_sizes.append(size)
        batch, self.result = self.result[:size], self.result[size:]
        return batch


class DestinationCursor:
    def __init__(self, stored):
        self.stored = stored
        self.pending = set()
        self.rowcount = 0
        self.fail_after = None
        self.writes = 0

    def execute(self, sql, parameters):
        assert sql.startswith('INSERT IGNORE')
        self.writes += 1
        if self.fail_after is not None and self.writes > self.fail_after:
            raise RuntimeError('Insert failed')
        identity = (parameters[0], parameters[3])
        self.rowcount = int(identity not in self.stored and identity not in self.pending)
        self.pending.add(identity)


class IncrementalArchiveTests(unittest.TestCase):
    def setUp(self):
        self.time = datetime(2026, 10, 9, 8, 0)
        self.stored = set()
        self.config = SimpleNamespace(log_types=('error_log',), custom_sources=(), retention_months=12, batch_size=2, archive_db='archivedb', archive_table='logs')

    def run_cycle(self, rows, state=None, fail_after=None):
        read = SourceCursor(rows)
        write = DestinationCursor(self.stored)
        write.fail_after = fail_after
        source = unittest.mock.Mock()
        source.cursor.return_value = read
        archive = unittest.mock.Mock()
        archive.cursor.return_value = write
        archive.commit.side_effect = lambda: self.stored.update(write.pending)

        @contextmanager
        def connection(value):
            yield value

        with patch('modules.archive_service.ensure_schema'), patch('modules.archive_service.load_state', return_value=state or {}), patch('modules.archive_service.source_connection', return_value=connection(source)), patch('modules.archive_service.archive_connection', return_value=connection(archive)):
            try:
                result = archive_error_log(self.config)
            except Exception:
                archive.commit.assert_not_called()
                raise
        source.cursor.assert_called_once_with(dictionary=True, buffered=False)
        archive.commit.assert_called_once()
        return result, read

    def row(self, message, when=None):
        return {'LOGGED': when or self.time, 'DATA': message}

    def test_ties_cross_multiple_batches_without_gaps(self):
        rows = [self.row(str(i)) for i in range(7)] + [self.row('newer', self.time + timedelta(seconds=1))]
        (copied, cursors), read = self.run_cycle(rows, {'source_cursors': {'error_log': str(self.time)}})
        self.assertEqual(copied, 8)
        self.assertEqual(len(self.stored), 8)
        self.assertEqual(cursors['error_log'], str(self.time + timedelta(seconds=1)))
        self.assertEqual(len(read.queries), 1)
        self.assertTrue(all(size == 2 for size in read.fetch_sizes))

    def test_new_boundary_row_is_archived_and_repeats_are_ignored(self):
        rows = [self.row('existing')]
        (copied, cursors), _ = self.run_cycle(rows)
        self.assertEqual(copied, 1)
        (copied, cursors), _ = self.run_cycle(rows + [self.row('late boundary')], {'source_cursors': cursors})
        self.assertEqual(copied, 1)
        self.assertEqual(len(self.stored), 2)
        (copied, _), read = self.run_cycle(rows + [self.row('late boundary')], {'source_cursors': cursors})
        self.assertEqual(copied, 0)
        self.assertEqual(len(read.queries), 1)

    def test_restart_with_old_checkpoint_is_idempotent(self):
        rows = [self.row(str(i)) for i in range(5)]
        self.run_cycle(rows)
        (copied, _), _ = self.run_cycle(rows)
        self.assertEqual(copied, 0)
        self.assertEqual(len(self.stored), 5)

    def test_full_duplicate_batch_does_not_stop_new_rows(self):
        existing = [self.row('existing-1'), self.row('existing-2')]
        (_, cursors), _ = self.run_cycle(existing)
        with self.assertLogs(level='INFO') as logs:
            (copied, _), read = self.run_cycle(existing + [self.row('new-1'), self.row('new-2'), self.row('new-3')], {'source_cursors': cursors})
        self.assertEqual(copied, 3)
        self.assertEqual(len(self.stored), 5)
        self.assertEqual(len(read.queries), 1)
        self.assertTrue(any('read=2 inserted=0 ignored=2 batch_size=2' in line for line in logs.output))

    def test_empty_result_preserves_checkpoint(self):
        state = {'source_cursors': {'error_log': str(self.time), 'other': 'saved'}}
        (copied, cursors), _ = self.run_cycle([], state)
        self.assertEqual(copied, 0)
        self.assertEqual(cursors, state['source_cursors'])

    def test_old_rows_are_filtered_out(self):
        (copied, _), _ = self.run_cycle([self.row('older', self.time - timedelta(seconds=1)), self.row('boundary')], {'source_cursors': {'error_log': str(self.time)}})
        self.assertEqual(copied, 1)

    def test_failed_write_does_not_commit_or_mutate_checkpoint(self):
        state = {'source_cursors': {'error_log': str(self.time)}}
        with self.assertRaisesRegex(RuntimeError, 'Insert failed'):
            self.run_cycle([self.row('first'), self.row('second')], state, fail_after=1)
        self.assertEqual(self.stored, set())
        self.assertEqual(state['source_cursors']['error_log'], str(self.time))


if __name__ == '__main__':
    unittest.main()
