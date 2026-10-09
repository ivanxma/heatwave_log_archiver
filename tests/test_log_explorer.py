"""Archive browsing must be bounded, authenticated and expand JSON safely."""
import json
from contextlib import contextmanager
from unittest.mock import patch

import unittest
from tests import test_navigation as navigation
from modules.log_explorer import flatten, sort_expression, search_clause, fill_buckets


class ExplorerTests(unittest.TestCase):
    save = navigation.NavigationTests.save
    # Reuse login fixtures, without rerunning the navigation cases in this class.
    def setUp(self):
        navigation.NavigationTests.setUp(self)
        self.settings.update(archive_connections=[{'name': 'archive', 'host': 'db', 'user': 'admin', 'secret_ocid': 'ocid1.vaultsecret.test'}], archive_tables=[{'name': 'logs', 'connection': 'archive', 'archive_db': 'logs', 'archive_table': 'entries'}])

    def test_expands_nested_objects_arrays_and_preserves_scalar_types(self):
        row = flatten({'id': 1, 'payload': '{"message":"test","nested":{"code":5},"tags":["a","b"]}'}, {'payload'})
        self.assertEqual(row[('payload', 'nested', 'code')], 5)
        self.assertEqual(row[('payload', 'tags', 1)], 'b')
        self.assertNotIn(('payload',), row)

    def test_sort_and_search_bind_values(self):
        expression, params = sort_expression(json.dumps(['payload', 'quoted"name']), {'payload': 'json'})
        self.assertEqual(expression, 'JSON_EXTRACT(`payload`, %s)')
        self.assertIn('quoted', params[0])
        with self.assertRaises(ValueError):
            sort_expression('["id;DROP TABLE t"]', {'id': 'bigint'})
        where, params = search_clause('100%_!', {'payload': 'json'})
        self.assertEqual(params, ('%100!%!_!!%',))
        self.assertIn('LIKE %s', where)

    def test_menu_without_selection_never_resolves_vault(self):
        with patch('modules.log_explorer.vault_credential', side_effect=AssertionError('Vault')):
            response = self.client.get('/log-explore')
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'Log Explore', response.data)

    def fake_connection(self, buckets=False):
        class Cursor:
            def execute(inner, sql, params=()):
                self.sql.append((sql, params))
            def fetchall(inner):
                sql = self.sql[-1][0]
                if sql.startswith('SHOW'):
                    return [{'Field': 'id', 'Type': 'bigint'}, {'Field': 'event_time', 'Type': 'datetime'}, {'Field': 'payload', 'Type': 'json'}]
                if buckets:
                    return [{'bucket': '2026-10-09', 'records': 3}]
                return [{'id': 1, 'event_time': '2026-10-09', 'payload': '{"message":"<script>alert(1)</script>","code":3}'}]
            def close(inner):
                pass
        class DB:
            def cursor(inner, **kwargs):
                return Cursor()
        @contextmanager
        def connect(_):
            yield DB()
        self.sql = []
        return patch('modules.log_explorer.connect', side_effect=connect)

    def test_page_expands_fields_escapes_html_and_limits_fetch(self):
        with self.fake_connection():
            response = self.client.get('/log-explore?connection=archive&table=logs&size=25&q=test')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'payload.message', response.data)
        self.assertNotIn(b'<script>alert(1)</script>', response.data)
        self.assertIn('LIMIT %s OFFSET %s', self.sql[-1][0])
        self.assertEqual(self.sql[-1][1][-2:], (26, 0))

    def test_csv_expands_json_and_chart_aggregates_in_db(self):
        with self.fake_connection():
            response = self.client.get('/log-explore?connection=archive&table=logs&download=csv')
            self.assertEqual(response.mimetype, 'text/csv')
            self.assertIn(b'payload.message', response.data)
        with self.fake_connection(buckets=True):
            response = self.client.get('/log-explore?connection=archive&table=logs&view=chart&interval=day&start=2026-10-09&end=2026-10-09')
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'3 records', response.data)
            self.assertIn('COUNT(*)', self.sql[-1][0])
            self.assertIn('`event_time` >= %s', self.sql[-1][0])

    def test_invalid_selection_does_not_open_connection(self):
        with patch('modules.log_explorer.connect') as connect:
            response = self.client.get('/log-explore?connection=archive&table=unknown')
            self.assertIn(b'Select a configured archive', response.data)
            connect.assert_not_called()
        with __import__('app').app.test_client() as client:
            self.assertEqual(client.get('/log-explore').status_code, 302)

    def test_chart_gaps_and_bucket_limit(self):
        from datetime import date
        rows = fill_buckets([{'time': '2026-10-09', 'count': 2}], date(2026, 10, 8), date(2026, 10, 10), 'day')
        self.assertEqual([row['count'] for row in rows], [0, 2, 0])
        with self.assertRaisesRegex(ValueError, '1,000'):
            fill_buckets([], date(2026, 1, 1), date(2026, 10, 1), 'hour')
