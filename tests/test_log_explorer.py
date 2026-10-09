"""Archive browsing must be bounded, authenticated and expand JSON safely."""
import json
from contextlib import contextmanager
from unittest.mock import patch

import unittest
from tests import test_navigation as navigation
from modules.log_explorer import flatten, sort_expression, fill_buckets
from modules.explorer_filters import compile_filters


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
        where, params = compile_filters([{'field':'["payload"]', 'op':'contains', 'value':'100%_!'}], 'all', [], {'payload': 'json'})
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
            response = self.client.get('/log-explore?connection=archive&table=logs&size=25&rule_field=%5B%22payload%22%2C%22message%22%5D&rule_op=contains&rule_value=test')
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

    def test_rules_sources_and_values_survive_pagination_links(self):
        with self.fake_connection():
            response = self.client.get('/log-explore', query_string=[('connection','archive'),('table','logs'),('rule_field','["payload","code"]'),('rule_op','gte'),('rule_value','2'),('rule_field','["payload","message"]'),('rule_op','contains'),('rule_value','script')])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data.count(b'name="rule_field" value='), 2)
        self.assertIn(b'Filter rules', response.data)
        self.assertNotIn(b'name="q"', response.data)
        self.assertIn(b'SHA-256', response.data)

    def test_filters_reject_untrusted_roots_and_missing_metadata(self):
        with self.assertRaises(ValueError):
            compile_filters([{'field':'["evil`sql"]', 'op':'eq', 'value':'x'}], 'all', [], {'payload':'json'})
        with self.assertRaisesRegex(ValueError, 'no source UUID'):
            compile_filters([], 'all', ['__unknown__'], {'payload':'json'})
        expression, params = compile_filters([{'field':'["payload","missing"]', 'op':'is_null', 'value':''}], 'all', [], {'payload':'json'})
        self.assertEqual(expression.count('%s'), len(params))
        self.assertEqual(params[-1], 'NULL')

    def test_optional_source_rule_expands_cluster_uuid_history(self):
        profiles=[{'name':'cluster','server_uuid':'22222222-2222-2222-2222-222222222222','server_uuids':['11111111-1111-1111-1111-111111111111','22222222-2222-2222-2222-222222222222']}]
        expression, params = compile_filters([{'field':'__source_connection__','op':'eq','value':'["cluster"]'}], 'all', [], {'source_server_uuid':'char(36)'}, profiles)
        self.assertIn('IN (%s,%s)', expression)
        self.assertEqual(len(params), 2)
        from werkzeug.datastructures import MultiDict
        from modules.explorer_filters import parse_rules
        self.assertEqual(parse_rules(MultiDict([('rule_field','__source_connection__'),('rule_op','eq'),('rule_value','[]')])), [])
        self.assertEqual(compile_filters([], 'all', [], {'payload':'json'}), ('', ()))

    def test_binary_fingerprint_is_displayed_as_hex(self):
        self.assertEqual(flatten({'source_fingerprint':bytes.fromhex('ab'*32)}, set())[('source_fingerprint',)], 'ab'*32)
